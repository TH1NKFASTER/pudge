"""The window API's iPhone/iPad companion part: phone access on/off, the HTTPS server and its
Bonjour advertisement, pairing (QR invitation, Allow/Deny), paired devices, and audiobook
handoff between the Mac and a phone.

``IPhoneApiMixin`` is a base of ``WebAppApi``: its public methods are window bridge calls like
any other API method. It uses the API's ``manager``, ``config``, ``audiobooks``, ``logger`` and
``_push_ui``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
import shutil
import subprocess
import sys
import threading
import time
import weakref
from pathlib import Path
from typing import Any

from .i18n import err

# APIs that advertise the phone server over Bonjour (dns-sd processes + a 30 s re-check).
_IPHONE_BONJOUR_OWNERS: "weakref.WeakSet[Any]" = weakref.WeakSet()


def _network_of(address: str) -> str:
    """The /24 (IPv4) a LAN address belongs to: one Bonjour name per network, not per lease."""
    try:
        return str(ipaddress.ip_network(f"{address}/24", strict=False))
    except ValueError:
        return str(address or "")


def stop_iphone_bonjour_everywhere() -> None:
    """Stop every Bonjour advertisement and its re-check in this process (test teardown)."""
    for owner in list(_IPHONE_BONJOUR_OWNERS):
        try:
            owner._stop_iphone_bonjour()
        except Exception:  # noqa: BLE001 - best effort at teardown
            pass


class IPhoneApiMixin:
    """iPhone/iPad companion calls of the window API (see the module docstring)."""

    @staticmethod
    def _lan_hosts() -> list[str]:
        """Addresses for the pairing QR, best first: private LAN interfaces only (no VPN tunnel,
        no public address), so a full-tunnel VPN or Ethernet + Wi-Fi still give a reachable one."""
        from . import iphone_network

        return iphone_network.lan_hosts() or ["127.0.0.1"]

    def _remove_web_companion_leftovers(self) -> None:
        """The browser companion was removed: drop its video cache once (HLS segments, registry rows)."""
        try:
            leftover = Path(self.config.paths.cache_dir).expanduser() / "companion-hls"
            if leftover.is_dir():
                shutil.rmtree(leftover, ignore_errors=True)
                self.logger.info("EVENT companion.removed_cache path=%s", leftover)
            with self.manager.db.connect() as conn:
                conn.execute("DELETE FROM cache_registry WHERE category='hls'")
                # Statistics replication queue for companion clients (nothing reads it any more).
                conn.execute("DELETE FROM consumption_sync_outbox")
                conn.execute("DELETE FROM consumption_sync_receipts")
        except Exception as exc:  # noqa: BLE001 - housekeeping must never break startup
            self.logger.warning("SKIP step=companion.cleanup error=%r", str(exc))

    def _configure_iphone(self) -> None:
        service = getattr(self, "_iphone_service", None)
        if service is not None and service.db is not self.manager.db:
            # The library database was reopened (restore): it may come from an older backup.
            service.db = self.manager.db
            service.ensure_schema()
        if self.manager.db.get_state("iphone_enabled", "0") == "1":
            try:
                self._ensure_iphone_server()
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                self.logger.warning("iPhone service unavailable: %s", type(exc).__name__)

    def _ensure_iphone_server(self) -> None:
        service = getattr(self, "_iphone_service", None)
        if getattr(self, "_iphone_server", None) is not None:
            if service is not None:
                service.api = self
            if service is not None and service.audiobooks is not self.audiobooks:
                # The audiobook service was rebuilt (settings reload): keep
                # live Mac sessions attached to the same protocol state.
                service.audiobooks = self.audiobooks
                self.audiobooks.mobile_progress = service
            return
        from .iphone_service import IPhoneService
        from .iphone_http import start_iphone_server

        if service is None:
            # Archived phone audio can be gigabytes: keep it beside the library
            # database (data dir), never in the config directory. One instance
            # per app run, so toggling access never forgets live Mac sessions.
            service = IPhoneService(
                self.manager.db, Path(self.config.library.database_path).expanduser().parent / "iphone"
            )
            # Light-novel covers ("covers/<file>") live in the library cover cache.
            service.cover_dirs = [Path(self.config.library.cover_cache_dir).expanduser()]
        server, thread, pin = start_iphone_server(service)
        service.audiobooks = self.audiobooks
        service.api = self
        self.audiobooks.mobile_progress = service
        self._iphone_service = service
        if self.manager.db.get_state("iphone.listening_backfill.v1") != "1":

            def backfill() -> None:
                try:
                    count = service.backfill_listening_runs()
                    self.manager.db.set_state("iphone.listening_backfill.v1", "1")
                    self.logger.info("EVENT iphone.listening_backfill runs=%s", count)
                except Exception as exc:  # noqa: BLE001 - statistics repair is best effort
                    self.logger.warning("FAIL iphone.listening_backfill error=%r", str(exc)[:300])

            threading.Thread(target=backfill, name="pudge-iphone-listen-backfill", daemon=True).start()
        self._iphone_server = server
        self._iphone_thread = thread
        self._iphone_pin = pin
        self._iphone_invitation = None
        self._iphone_bonjour = {}
        self._iphone_bonjour_lock = getattr(self, "_iphone_bonjour_lock", None) or threading.Lock()
        stop = threading.Event()
        self._iphone_bonjour_stop = stop
        _IPHONE_BONJOUR_OWNERS.add(self)
        self._advertise_iphone()
        if sys.platform == "darwin" and shutil.which("dns-sd"):

            def watch() -> None:
                # Networks change while Pudge runs (Wi-Fi, VPN): advertise where the Mac is now.
                while not stop.wait(30):
                    try:
                        self._advertise_iphone()
                    except Exception as exc:  # noqa: BLE001 - discovery is optional
                        self.logger.warning("SKIP step=iphone.bonjour error=%r", str(exc)[:200])

            threading.Thread(target=watch, name="pudge-iphone-bonjour", daemon=True).start()

    def _advertise_iphone(self) -> None:
        """Bonjour for paired phones on private LAN interfaces only (dns-sd per interface).

        Nothing is advertised while no iPhone/iPad is paired (pairing uses the QR's addresses),
        and the name is new for each network: it was the permanent server ID, which let anyone
        on a café or hotel Wi-Fi recognise the same Mac wherever it went. Paired phones find
        the Mac by trying the Pudge services they see (only it passes their certificate pin).
        While a paired device still runs an older build (it accepts only the server ID as the
        name), that legacy advertisement stays too, until every device was seen on a newer one.
        """
        server = getattr(self, "_iphone_server", None)
        service = getattr(self, "_iphone_service", None)
        if server is None or service is None or sys.platform != "darwin" or not shutil.which("dns-sd"):
            return
        from . import iphone_network

        try:
            needs = dict(service.bonjour_needs())
        except Exception:  # noqa: BLE001 - an unreadable library advertises nothing
            needs = {}
        private, legacy = bool(needs.get("private")), bool(needs.get("legacy"))
        interfaces = iphone_network.lan_interfaces() if private or legacy else []
        wanted = {name: address for name, address in interfaces if name}
        lock = getattr(self, "_iphone_bonjour_lock", None) or threading.Lock()
        with lock:
            stop = getattr(self, "_iphone_bonjour_stop", None)
            if stop is not None and stop.is_set():
                return  # access was turned off meanwhile
            self._advertise_on(service, server, wanted, private=private, legacy=legacy)

    @staticmethod
    def _bonjour_name(service: Any, network: str) -> str:
        """The advertised name on one network: the same there (no churn every 30 s), unrelated
        between networks and to the server ID; a new one after each launch of Pudge."""
        salt = getattr(IPhoneApiMixin, "_bonjour_salt", None)
        if salt is None:
            salt = IPhoneApiMixin._bonjour_salt = secrets.token_bytes(16)
        value = hmac.new(salt, (str(service.server_id) + ":" + network).encode(), hashlib.sha256)
        return "Pudge " + value.hexdigest()[:12]

    def _advertise_on(
        self,
        service: Any,
        server: Any,
        wanted: dict[str, str] | set[str],
        *,
        private: bool = True,
        legacy: bool = False,
    ) -> None:
        if not isinstance(wanted, dict):
            wanted = {name: "" for name in wanted}
        # The private advertisement: one per interface, network (its address) and day (a new
        # network or a new day, a new name). The legacy one ("<interface>#legacy"): the server
        # ID as the name and in TXT, as older phone builds look for.
        day = time.strftime("%Y-%m-%d", time.gmtime())
        desired: dict[str, tuple[str, str, list[str]]] = {}
        for name, address in wanted.items():
            if private:
                key = name + "@" + _network_of(address) + "@" + day
                desired[name] = (name, key, [self._bonjour_name(service, key), "v=2"])
            if legacy:
                desired[name + "#legacy"] = (
                    name,
                    name + "#legacy",
                    [str(service.server_id), "serverId=" + str(service.server_id)],
                )
        current = dict(getattr(self, "_iphone_bonjour", None) or {})
        networks = dict(getattr(self, "_iphone_bonjour_networks", None) or {})
        for entry, process in list(current.items()):
            if entry not in desired or process.poll() is not None or networks.get(entry) != desired[entry][1]:
                if process.poll() is None:
                    process.terminate()
                del current[entry]
                networks.pop(entry, None)
        for entry in sorted(set(desired) - set(current)):
            interface, key, (instance, txt) = desired[entry][0], desired[entry][1], desired[entry][2]
            current[entry] = subprocess.Popen(
                [
                    "dns-sd",
                    "-i",
                    interface,
                    "-R",
                    instance,
                    "_pudge-audio._tcp",
                    "local",
                    str(server.server_port),
                    txt,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            networks[entry] = key
            self.logger.info(
                "EVENT iphone.bonjour interface=%s kind=%s",
                interface,
                "legacy" if entry.endswith("#legacy") else "private",
            )
        self._iphone_bonjour = current
        self._iphone_bonjour_networks = networks

    def _stop_iphone_bonjour(self) -> None:
        """End the Bonjour re-check and the dns-sd processes."""
        with getattr(self, "_iphone_bonjour_lock", None) or threading.Lock():
            stop = getattr(self, "_iphone_bonjour_stop", None)
            if stop is not None:
                stop.set()
            for process in (getattr(self, "_iphone_bonjour", None) or {}).values():
                if process.poll() is None:
                    process.terminate()
            self._iphone_bonjour = {}
            self._iphone_bonjour_networks = {}

    def _stop_iphone_server(self, *, strict: bool = True) -> None:
        """Stop the phone server. Open connections are cut first (the phone resumes a download
        or upload later). `strict` (quit, restore): a request still running afterwards keeps the
        server up and raises; turning access off in Settings closes the server anyway."""
        server = getattr(self, "_iphone_server", None)
        if server is None:
            return
        server.shutdown()
        server.closing = True
        server.close_requests()
        if not server.wait_for_requests(timeout=5):
            if strict:
                server.closing = False
                thread = threading.Thread(target=server.serve_forever, name="pudge-iphone-https", daemon=True)
                thread.start()
                self._iphone_thread = thread
                raise RuntimeError(err("iphone.deviceTransfersActive"))
            self.logger.warning("SKIP step=iphone.stop reason=requests_still_running")
        server.server_close()
        service = getattr(self, "_iphone_service", None)
        if service is not None and service.http_server is server:
            service.http_server = None
        self._stop_iphone_bonjour()
        self._iphone_server = None
        self._iphone_invitation = None
        # The protocol service stays attached: Mac listening while access is
        # off is still recorded, so re-enabling never resumes an older place.

    def iphone_enable(self) -> dict[str, Any]:
        self._ensure_iphone_server()
        self.manager.db.set_state("iphone_enabled", "1")
        return self.iphone_status()

    def iphone_disable(self) -> dict[str, Any]:
        self._stop_iphone_server(strict=False)
        self.manager.db.set_state("iphone_enabled", "0")
        return self.iphone_status()

    def iphone_status(self) -> dict[str, Any]:
        self._configure_iphone()
        service = getattr(self, "_iphone_service", None)
        enabled = getattr(self, "_iphone_server", None) is not None
        invitation = getattr(self, "_iphone_invitation", None)
        claimed = False
        claimed_device = ""
        if service is not None and invitation is not None:
            with service.db.connect() as conn:
                row = conn.execute(
                    "SELECT device,approval FROM iphone_invites WHERE hash=?",
                    (hashlib.sha256(invitation["invitation"].encode()).hexdigest(),),
                ).fetchone()
            # Connected only once allowed here (a device waiting for Allow/Deny is in "pending").
            claimed = bool(row and row["device"] and row["approval"] in (None, "approved"))
            claimed_device = str(row["device"]) if claimed else ""
            if row and row["approval"] == "denied":
                self._iphone_invitation = invitation = None
        return {
            "enabled": enabled,
            "devices": service.devices() if service else [],
            "pending": service.pending_pairings() if service and enabled else [],
            "paired": claimed,
            # Several iPhones and iPads can be paired: the message names the one that just did.
            "pairedName": service.device_name(claimed_device) if claimed else None,
            "expiresAt": invitation["expiresAt"] if invitation else None,
        }

    def iphone_device_copies(self) -> dict[str, Any]:
        """Which paired iPhones/iPads have which library items downloaded (their own last
        reports; also while phone access is off: the copies stay on the devices)."""
        from .iphone_inventory import summary

        try:
            return summary(self.manager.db)
        except Exception as exc:  # noqa: BLE001 - the icons are optional, the library is not
            self.logger.warning("SKIP step=iphone.device_copies error=%r", str(exc)[:200])
            return {"devices": [], "anime": {}, "ln": {}, "manga": {}, "audiobooks": {}}

    def iphone_start_pairing(self) -> dict[str, Any]:
        from .iphone_service import encoded
        from ._qr import QrCode

        self.iphone_enable()
        self.iphone_cancel_pairing()
        server = self._iphone_server
        payload = self._iphone_service.invite(
            [f"https://{host}:{server.server_port}" for host in self._lan_hosts()], self._iphone_pin
        )
        self._iphone_invitation = payload
        link = "pudge-audio://pair?data=" + base64.urlsafe_b64encode(
            encoded(payload).encode()
        ).decode().rstrip("=")
        code = QrCode.encode_text(link, QrCode.Ecc.MEDIUM)
        size = code.get_size() + 8
        cells = " ".join(
            f"M{x + 4},{y + 4}h1v1h-1z"
            for y in range(code.get_size())
            for x in range(code.get_size())
            if code.get_module(x, y)
        )
        svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" shape-rendering="crispEdges"><rect width="100%" height="100%" fill="white"/><path d="{cells}" fill="black"/></svg>'
        return {
            "invitation": link,
            "qrDataURL": "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode(),
            "expiresAt": payload["expiresAt"],
            "serverName": payload["serverName"],
        }

    def iphone_cancel_pairing(self) -> dict[str, Any]:
        payload = getattr(self, "_iphone_invitation", None)
        service = getattr(self, "_iphone_service", None)
        if payload is not None and service is not None:
            service.cancel_invitation(payload["invitation"])
        self._iphone_invitation = None
        return {"ok": True}

    def iphone_revoke_device(self, device_id: str) -> dict[str, Any]:
        if getattr(self, "_iphone_service", None) is None:
            self._ensure_iphone_server()
        # With access turned off the service stays attached (Settings still lists the devices):
        # revoking there started the HTTPS server and its Bonjour advertisement again.
        self._iphone_service.revoke(str(device_id))
        self.logger.info("EVENT iphone.revoked device=%s", str(device_id)[:8])
        self._readvertise_iphone()
        return self.iphone_status()

    def _readvertise_iphone(self) -> None:
        """Paired devices changed: Bonjour starts with the first one and ends with the last."""
        try:
            self._advertise_iphone()
        except Exception as exc:  # noqa: BLE001 - discovery is optional
            self.logger.warning("SKIP step=iphone.bonjour error=%r", str(exc)[:200])

    def iphone_approve_pairing(self, device_id: str) -> dict[str, Any]:
        """Allow: the iPhone/iPad showing the same code receives its access."""
        service = getattr(self, "_iphone_service", None)
        if service is not None and service.approve_pairing(str(device_id)):
            self.logger.info("EVENT iphone.pairing_allowed device=%s", str(device_id)[:8])
            self._readvertise_iphone()
        return self.iphone_status()

    def iphone_deny_pairing(self, device_id: str) -> dict[str, Any]:
        service = getattr(self, "_iphone_service", None)
        if service is not None:
            service.deny_pairing(str(device_id))
            self.logger.info("EVENT iphone.pairing_denied device=%s", str(device_id)[:8])
        return self.iphone_status()

    def iphone_resume_on_mac(self, book_id: int, operation_id: str, speed: float = 1.0) -> dict[str, Any]:
        service = getattr(self, "_iphone_service", None)
        if service is None or getattr(self, "_iphone_server", None) is None:
            return {"status": "disabled"}
        result = service.request_mac_handoff(int(book_id), str(operation_id))
        if result["status"] != "source_paused":
            return result
        if service.claim_mac_handoff(str(operation_id)):
            try:
                playback = self.audiobooks.play(
                    int(book_id), start=result["positionMs"] / 1000, speed=float(speed)
                )
                self._push_ui("audio", {"players": [[int(book_id), True]]})
                with service.db.connect() as conn:
                    conn.execute("UPDATE iphone_mac_starts SET started=1 WHERE operation=?", (operation_id,))
                return {**result, "targetStatus": "started", **playback}
            except (OSError, ValueError, RuntimeError):
                with service.db.connect() as conn:
                    conn.execute("UPDATE iphone_mac_starts SET started=-1 WHERE operation=?", (operation_id,))
                return {**result, "targetStatus": "start_failed"}
        with service.db.connect() as conn:
            state = conn.execute(
                "SELECT started FROM iphone_mac_starts WHERE operation=?", (operation_id,)
            ).fetchone()
        return {
            **result,
            "targetStatus": "started"
            if state and state[0] == 1
            else "start_failed"
            if state and state[0] == -1
            else "start_unknown",
        }

    def iphone_cancel_handoff(self, operation_id: str) -> dict[str, Any]:
        service = getattr(self, "_iphone_service", None)
        if service is not None:
            service.cancel_mac_handoff(str(operation_id))
        return {"ok": True}

    def _iphone_audio_state(self, state: dict[str, Any]) -> dict[str, Any]:
        service = getattr(self, "_iphone_service", None)
        if service is not None and getattr(self, "_iphone_server", None) is not None:
            for book in state.get("books") or []:
                asset = service.asset_for_book(int(book["id"]))
                progress = service.progress(asset) if asset else None
                book["iphone_progress"] = progress
                book["iphone_live"] = service.playing_elsewhere(progress, "mac")
                selected = (progress or {}).get("selected") or {}
                book["iphone_live_name"] = (
                    service.device_name(str(selected["deviceId"])) if book["iphone_live"] else None
                )
                # One mark per iPhone/iPad whose position differs; any of them resolves for all.
                book["iphone_positions"] = service.phone_position_choices(progress)
                book["iphone_position"] = (book["iphone_positions"] or [None])[0]
        return state

    def audiobook_use_iphone_position(
        self, book_id: int, checkpoint_id: str, expected_revision: int
    ) -> dict[str, Any]:
        """The yellow iPhone mark on the seek bar: take the phone's position (resolves the conflict)."""
        service = getattr(self, "_iphone_service", None)
        if service is None or getattr(self, "_iphone_server", None) is None:
            return {"status": "disabled"}
        book_id = int(book_id)
        position = service.use_phone_position(book_id, str(checkpoint_id), int(expected_revision))
        self.logger.info("RESULT step=iphone.use_phone_position book=%s position=%.1f", book_id, position)
        if self.audiobooks.is_playing(book_id):
            self.audiobooks.seek_to(book_id, position)
        state = self._iphone_audio_state({"books": [self.audiobooks.book(book_id)]})
        return {"ok": True, "book": state["books"][0]}
