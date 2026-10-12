import AppKit

// NSApplication holds its delegate weakly, so the delegate must be owned by something that
// outlives app.run(): a global, not a local that Swift may release after its last use.
// Top-level code is not main-actor isolated in Swift 5 mode, while AppDelegate is (it
// conforms to SwiftTerm's @MainActor delegate protocol), so it is created on the actor.
let delegate = MainActor.assumeIsolated { AppDelegate() }

MainActor.assumeIsolated {
    let app = NSApplication.shared
    app.delegate = delegate
    app.setActivationPolicy(.regular)
    app.run()
}
