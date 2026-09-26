"""Unified torrent UI must distinguish cached metadata from observed traffic."""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge import web_app

ROOT = Path(__file__).resolve().parents[1]


def test_downloads_api_only_marks_successful_backend_refresh_as_observed(monkeypatch) -> None:
    api = object.__new__(web_app.WebAppApi)
    api.manager = SimpleNamespace(
        db=SimpleNamespace(anime_list=lambda: [], downloads=lambda: []),
        sync_downloads=lambda: None,
        torrent_backend_name=lambda: "aria2",
    )
    api._downloads_configured = lambda: True
    api._downloads_enabled = lambda: True
    api._storage_payload = lambda refresh=False: {}
    api._last_network_guard = {}
    api.config = SimpleNamespace(aria2=SimpleNamespace(enabled=False))
    api.logger = logging.getLogger("test-torrent-unified-f1")
    monkeypatch.setattr(web_app.time, "time", lambda: 1234567890.0)

    local = api.torrent_downloads(refresh=False)
    assert local["observed_at"] is None
    assert local["downloads"] == []

    observed = api.torrent_downloads(refresh=True)
    assert observed["observed_at"] == 1234567890.0
    assert observed["warning"] == ""

    def fail():
        raise TimeoutError("backend unavailable")

    api.manager.sync_downloads = fail
    failed = api.torrent_downloads(refresh=True)
    assert failed["observed_at"] is None
    assert failed["warning"] == "backend unavailable"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required for UI runtime tests")
def test_live_ui_statuses_and_rates_in_three_screens() -> None:
    script = r"""
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync('pudge/web/index.html','utf8');
const helper=fs.readFileSync('pudge/web/torrent_status_ui.js','utf8');
const home=fs.readFileSync('pudge/web/home_status.js','utf8');
const fixed=1900000000;
const now=()=>fixed;
const sandbox={Date:{now:()=>fixed*1000},console};
vm.createContext(sandbox);
vm.runInContext(helper,sandbox);
const state=(raw,progress=.4)=>({state:raw,progress});
assert.equal(sandbox.torrentUiState(state('queuedDL'),true),'waiting');
assert.equal(sandbox.torrentUiState(state('stalledDL'),true),'stalled');
assert.equal(sandbox.torrentUiState(state('pausedDL'),true),'paused');
assert.equal(sandbox.torrentUiState(state('downloading'),true),'downloading');
assert.equal(sandbox.torrentUiState(state('uploading',1),true),'finished');
assert.equal(sandbox.torrentUiState(state('downloading',.999),true),'downloading');
assert.equal(sandbox.torrentUiState(state('downloading'),false,'unconfirmed'),'unconfirmed');
assert.equal(sandbox.torrentUiState(state('downloading'),false,'off_confirmed'),'paused');
assert.equal(sandbox.torrentUiState(state('missingFiles'),false,'off_confirmed'),'error');
assert.equal(sandbox.torrentTrafficFresh({enabled:true,stale:false,updated_at:fixed-14},true,fixed),true);
assert.equal(sandbox.torrentTrafficFresh({enabled:true,stale:false,updated_at:fixed-16},true,fixed),false);
assert.equal(sandbox.torrentTrafficFresh({enabled:true,stale:true,updated_at:fixed},true,fixed),false);
assert.equal(sandbox.torrentSnapshotFresh({enabled:true,observed_at:fixed-2}, {enabled:false,stale:false,updated_at:fixed},false,fixed),false);
assert.equal(sandbox.torrentSnapshotFresh({enabled:true,observed_at:fixed-2}, {enabled:true,stale:false,updated_at:fixed},true,fixed),true);
const elements={};
sandbox.$=id=>elements[id]??(elements[id]={innerHTML:'',classList:{contains:()=>true}});
sandbox.t=(key)=>key;
sandbox.escapeHtml=value=>String(value??'');
sandbox.torrentRate=value=>`${value} B/s`;
sandbox.torrentBytes=value=>`${value} bytes`;
sandbox.torrentEta=value=>`${value} sec`;
sandbox.torrentElapsed=()=>'-';
sandbox.animeFromId=()=>null;
sandbox.torrentToggleUiEnabled=()=>!!sandbox.ui.state.settings.torrents_enabled;
sandbox.ui={lang:'en',page:'downloads',torrentToggleDesired:null,downloadCenterFilter:'all',state:{settings:{torrents_enabled:true},downloads:[{name:'Queued',state:'queuedDL',progress:.2,download_speed:999,upload_speed:888}],subtitle_jobs:[],playlists:[],release_upgrades:[],subtitle_history:[]},torrentTraffic:{enabled:true,stale:true,updated_at:fixed,download_speed:999},downloadCenter:null};
const slice=(start,end)=>html.slice(html.indexOf(start),html.indexOf(end,html.indexOf(start)));
vm.runInContext(slice('function renderDownloads(){','function torrentBytes('),sandbox);
sandbox.renderDownloads();
assert.match(elements.downloadsContent.innerHTML,/Waiting: —/);
assert.match(elements.downloadsContent.innerHTML,/↓ —/);
assert.doesNotMatch(elements.downloadsContent.innerHTML,/999 B\/s/);
assert.match(elements.downloadsContent.innerHTML,/Queued<\/td>/);
sandbox.ui.torrentTraffic={enabled:true,stale:false,updated_at:fixed,waiting:1,download_speed:42,upload_speed:7};
sandbox.renderDownloads();
assert.match(elements.downloadsContent.innerHTML,/Waiting: 1/);
assert.match(elements.downloadsContent.innerHTML,/↓ 42 B\/s/);
assert.doesNotMatch(elements.downloadsContent.innerHTML,/999 B\/s/);
// Same state classifier controls Home's compact label and Download Center's filter.
sandbox.torrentEta=sec=>`${sec}s`;
vm.runInContext(home,sandbox);
assert.match(sandbox.compactDownloadStatus({state:'stalledDL',progress:.4}),/No connections/);
assert.match(sandbox.compactDownloadStatus({state:'queuedDL',progress:.4}),/Queued/);
assert.equal(sandbox.compactDownloadStatus({state:'pausedDL',progress:.4}),'Paused · 40%');
assert.equal(sandbox.compactDownloadStatus({state:'downloading',progress:.4}),'40%');
// A downloadable Home card labels only a currently observed active transfer.
sandbox.cover=()=>'';
sandbox.t=(key,args)=>key==='label.downloading'?`Downloading: ${args.progress}%`:key;
vm.runInContext(slice('function downloadAvailableHomeCard(a){','function caughtUpHomeCard(a){'),sandbox);
const cardData={media_id:42,title:'Sample',download:{state:'downloading',progress:.4,name:'Sample'}};
let card=sandbox.downloadAvailableHomeCard(cardData);
assert.match(card,/Downloading: 40%/);
assert.doesNotMatch(card,/class="download-card-button"/);
sandbox.ui.torrentTraffic={enabled:true,stale:true,updated_at:fixed};
card=sandbox.downloadAvailableHomeCard(cardData);
assert.doesNotMatch(card,/Downloading: 40%/);
sandbox.ui.torrentTraffic={enabled:false,stale:true,transition:'unconfirmed',updated_at:null};
sandbox.ui.state.settings.torrents_enabled=false;
card=sandbox.downloadAvailableHomeCard(cardData);
assert.match(card,/Shutdown not confirmed/);
sandbox.ui.state.settings.torrents_enabled=true;
sandbox.ui.torrentTraffic={enabled:true,stale:false,updated_at:fixed};

sandbox.ui.state.settings.torrents_enabled=false;
sandbox.ui.torrentTraffic={enabled:false,stale:true,transition:'unconfirmed',updated_at:null};
assert.match(sandbox.compactDownloadStatus({state:'downloading',progress:.4}),/Shutdown not confirmed/);
sandbox.ui.state.settings.torrents_enabled=true;
sandbox.ui.torrentTraffic={enabled:true,stale:false,updated_at:fixed,download_speed:42};
vm.runInContext(slice('function torrentPaused(state){','function torrentFilterMatch('),sandbox);
vm.runInContext(slice('function torrentFilterMatch(download,filter){','function renderDownloadCenter('),sandbox);
vm.runInContext(slice('function renderDownloadCenter(result=ui.downloadCenter){','function stopDownloadCenterPoll('),sandbox);
const row={hash:'x',name:'Episode',state:'stalledDL',progress:.4,download_speed:321,upload_speed:100,total_bytes:1000,downloaded_bytes:400,seeders:0,peers:0};
assert.equal(sandbox.torrentFilterMatch(row,'active'),false);
assert.equal(sandbox.torrentFilterMatch(row,'paused'),false);
assert.equal(sandbox.torrentFilterMatch(row,'all'),true);
sandbox.ui.downloadCenter={enabled:true,observed_at:null,downloads:[row],storage:{},network_guard:{}};
sandbox.renderDownloadCenter();
assert.match(elements.modalBody.innerHTML,/Active: —/);
assert.match(elements.modalBody.innerHTML,/Waiting: —/);
assert.doesNotMatch(elements.modalBody.innerHTML,/321 B\/s/);
sandbox.ui.downloadCenter.observed_at=fixed;
sandbox.renderDownloadCenter();
assert.match(elements.modalBody.innerHTML,/Active: 0/);
assert.match(elements.modalBody.innerHTML,/Waiting: 1/);
assert.match(elements.modalBody.innerHTML,/↓ 321 B\/s/);
sandbox.ui.state.settings.torrents_enabled=false;
sandbox.ui.torrentTraffic={enabled:false,stale:true,transition:'unconfirmed',updated_at:null};
sandbox.renderDownloadCenter();
assert.match(elements.modalBody.innerHTML,/Shutdown not confirmed/);
assert.match(elements.modalBody.innerHTML,/↓ —/);
assert.doesNotMatch(elements.modalBody.innerHTML,/321 B\/s/);
sandbox.ui.torrentTraffic={enabled:false,stale:false,transition:'off_confirmed',updated_at:fixed,download_speed:0,upload_speed:0};
sandbox.renderDownloadCenter();
assert.match(elements.modalBody.innerHTML,/Paused/);
assert.doesNotMatch(elements.modalBody.innerHTML,/321 B\/s/);
console.log('PASS unified Home, Activity, Download Center status and freshness');
"""
    result = subprocess.run(
        ["node", "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=20, check=False
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "PASS unified" in result.stdout
