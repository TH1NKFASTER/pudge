import Foundation

/// A side of the screen's interface orientation.
public enum ScreenSide: String, Equatable, Sendable {
    case portrait, portraitUpsideDown, landscapeLeft, landscapeRight
    public var isLandscape: Bool { self == .landscapeLeft || self == .landscapeRight }
}

/// The video player's own rotation lock: follow the device (either landscape side) or stay on
/// one landscape side.
public enum PlayerRotationLock: Equatable, Sendable {
    case followDevice
    case locked(ScreenSide)
}

/// Which orientations the app allows while the video player is (or is not) on screen.
/// On the iPhone, Control Center's rotation lock is a portrait lock: turned on while watching in
/// landscape it threw the player into portrait. The iPhone player is landscape on its own (as
/// video apps are), whatever that lock says; its lock button keeps one landscape side. The iPad
/// keeps the app's orientations (its rotation lock holds the current one).
public enum PlayerOrientation {
    public enum Allowed: Equatable, Sendable {
        /// The app's usual orientations (Info.plist).
        case app
        /// Both landscape sides (following the device between them).
        case landscape
        /// One side only.
        case only(ScreenSide)
    }

    public static func allowed(isPad: Bool, playerOnScreen: Bool, lock: PlayerRotationLock) -> Allowed {
        guard !isPad, playerOnScreen else { return .app }
        switch lock {
        case .followDevice: return .landscape
        case .locked(let side): return side.isLandscape ? .only(side) : .landscape
        }
    }

    /// The lock button: locked → follow the device; following → lock the landscape side shown
    /// now (the player still turning from portrait: the usual landscape side, home on the right).
    public static func toggled(_ lock: PlayerRotationLock, current: ScreenSide?) -> PlayerRotationLock {
        switch lock {
        case .locked: return .followDevice
        case .followDevice:
            if let current, current.isLandscape { return .locked(current) }
            return .locked(.landscapeRight)
        }
    }

    public static func isLocked(_ lock: PlayerRotationLock) -> Bool { lock != .followDevice }
}
