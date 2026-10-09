import AppKit

// Top-level code is not main-actor isolated in Swift 5 mode, while AppDelegate is
// (it conforms to SwiftTerm's @MainActor delegate protocol), so enter the actor first.
MainActor.assumeIsolated {
    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate
    app.setActivationPolicy(.regular)
    app.run()
}
