import AppKit
import Sparkle
import SwiftTerm

/// Colors and font for the terminal view. Textual paints every cell itself, so the
/// native colors only show before the monitor starts and in the exit message.
enum Theme {
    static let background = NSColor(srgbRed: 0.07, green: 0.07, blue: 0.08, alpha: 1)
    static let foreground = NSColor(srgbRed: 0.85, green: 0.86, blue: 0.88, alpha: 1)
    static let defaultFontSize: CGFloat = 13

    /// The chosen font if it is installed, else SF Mono, Menlo, or the system monospace.
    static func font(named name: String?, size: CGFloat) -> NSFont {
        if let name, let f = NSFont(name: name, size: size) { return f }
        for fallback in ["SFMono-Regular", "Menlo-Regular"] {
            if let f = NSFont(name: fallback, size: size) { return f }
        }
        return NSFont.monospacedSystemFont(ofSize: size, weight: .regular)
    }
}

/// Hosts the Python TUI (agent-farm-core, a PyInstaller build of main.py) inside a
/// SwiftTerm view and wires Sparkle for updates. Everything the monitor does still
/// happens in Python; this file only provides the window, the pty and the menus.
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, LocalProcessTerminalViewDelegate {
    private var window: NSWindow!
    private var terminal: LocalProcessTerminalView!
    private var updater: SPUStandardUpdaterController?
    private var coreRunning = false

    private let defaults = UserDefaults.standard
    private var fontSize: CGFloat {
        get { let v = defaults.double(forKey: "fontSize"); return v > 0 ? CGFloat(v) : Theme.defaultFontSize }
        set { defaults.set(Double(newValue), forKey: "fontSize") }
    }
    private var fontName: String? {
        get { defaults.string(forKey: "fontName") }
        set { defaults.set(newValue, forKey: "fontName") }
    }
    /// SwiftTerm's font smoothing (on = standard macOS rendering, off = thin strokes like iTerm2's setting).
    private var fontSmoothing: Bool {
        get { defaults.object(forKey: "fontSmoothing") as? Bool ?? true }
        set { defaults.set(newValue, forKey: "fontSmoothing") }
    }
    private var currentFont: NSFont { Theme.font(named: fontName, size: fontSize) }
    private weak var fontMenu: NSMenu?

    static let logDir = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/agent-farm")
    static let dataDir = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/agent-farm")

    // MARK: - lifecycle

    func applicationDidFinishLaunching(_ note: Notification) {
        setupUpdater()
        buildMenus()
        makeWindow()
        startCore()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ note: Notification) {
        if coreRunning { terminal.terminate() }
    }

    private func setupUpdater() {
        // Only from a real bundle with a feed URL; `swift run` during development has neither.
        guard Bundle.main.object(forInfoDictionaryKey: "SUFeedURL") != nil else { return }
        updater = SPUStandardUpdaterController(startingUpdater: true, updaterDelegate: nil, userDriverDelegate: nil)
    }

    private func makeWindow() {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 720),
                         styleMask: [.titled, .closable, .miniaturizable, .resizable],
                         backing: .buffered, defer: false)
        w.title = "agent-farm"
        w.minSize = NSSize(width: 520, height: 260)
        w.tabbingMode = .disallowed
        w.backgroundColor = Theme.background
        w.delegate = self
        w.center()
        w.setFrameAutosaveName("agent-farm.main")

        let t = LocalProcessTerminalView(frame: w.contentView!.bounds)
        t.autoresizingMask = [.width, .height]
        t.processDelegate = self
        t.nativeBackgroundColor = Theme.background
        t.nativeForegroundColor = Theme.foreground
        t.font = currentFont
        t.fontSmoothing = fontSmoothing
        w.contentView?.addSubview(t)
        terminal = t
        window = w

        w.makeKeyAndOrderFront(nil)
        w.makeFirstResponder(t)
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: - the Python core

    private func coreExecutable() -> String? {
        // AGENT_FARM_CORE lets a `swift run` during development point at any executable.
        if let p = ProcessInfo.processInfo.environment["AGENT_FARM_CORE"], !p.isEmpty { return p }
        guard let res = Bundle.main.resourceURL else { return nil }
        let p = res.appendingPathComponent("agent-farm-core/agent-farm-core").path
        return FileManager.default.isExecutableFile(atPath: p) ? p : nil
    }

    private func childEnvironment() -> [String] {
        // TERM, COLORTERM=truecolor (Textual needs it for 24-bit color), LANG, HOME, USER...
        var env = Terminal.getEnvironmentVariables(termName: "xterm-256color", trueColor: true)
        // Finder launches apps with a bare PATH; the monitor shells out to it2, ps, git and osascript.
        let inherited = ProcessInfo.processInfo.environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        let extra = ["/opt/homebrew/bin", "/usr/local/bin", "/Applications/iTerm.app/Contents/Resources/utilities"]
        env.append("PATH=" + (extra + [inherited]).joined(separator: ":"))
        env.append("TERM_PROGRAM=agent-farm")
        if let v = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String {
            env.append("AGENT_FARM_VERSION=\(v)")
        }
        return env
    }

    private func startCore() {
        guard let exe = coreExecutable() else {
            terminal.feed(text: "\r\n  agent-farm-core was not found inside the app bundle.\r\n")
            return
        }
        terminal.feed(text: "\u{1b}[2J\u{1b}[H")
        coreRunning = true
        terminal.startProcess(executable: exe, args: [], environment: childEnvironment(), execName: "agent-farm-core")
    }

    @objc func restartCore(_ sender: Any?) {
        if coreRunning {
            terminal.terminate()
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in self?.startCore() }
        } else {
            startCore()
        }
    }

    // MARK: - LocalProcessTerminalViewDelegate

    func sizeChanged(source: LocalProcessTerminalView, newCols: Int, newRows: Int) {}

    func setTerminalTitle(source: LocalProcessTerminalView, title: String) {
        DispatchQueue.main.async { [weak self] in self?.window.title = title.isEmpty ? "agent-farm" : title }
    }

    func hostCurrentDirectoryUpdate(source: TerminalView, directory: String?) {}

    func processTerminated(source: TerminalView, exitCode: Int32?) {
        coreRunning = false
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            if exitCode == 0 {               // the user pressed q inside the monitor
                NSApp.terminate(nil)
                return
            }
            let code = exitCode.map(String.init) ?? "?"
            let log = Self.logDir.appendingPathComponent("monitor.log").path
            self.terminal.feed(text: "\r\n\u{1b}[31m  agent-farm-core exited with code \(code)\u{1b}[0m\r\n"
                               + "  details: \(log)\r\n  \u{2318}R restarts the monitor, \u{2318}Q quits.\r\n")
        }
    }

    // MARK: - menus

    private func buildMenus() {
        let bar = NSMenu()

        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About agent-farm",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        let check = NSMenuItem(title: "Check for Updates…",
                               action: #selector(SPUStandardUpdaterController.checkForUpdates(_:)), keyEquivalent: "")
        check.target = updater
        appMenu.addItem(check)
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide agent-farm", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        let hideOthers = appMenu.addItem(withTitle: "Hide Others",
                                         action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h")
        hideOthers.keyEquivalentModifierMask = [.command, .option]
        appMenu.addItem(withTitle: "Show All", action: #selector(NSApplication.unhideAllApplications(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit agent-farm", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        bar.addItem(item("agent-farm", appMenu))

        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        bar.addItem(item("Edit", edit))

        let view = NSMenu(title: "View")
        view.addItem(withTitle: "Bigger Text", action: #selector(biggerText(_:)), keyEquivalent: "+")
        view.addItem(withTitle: "Smaller Text", action: #selector(smallerText(_:)), keyEquivalent: "-")
        view.addItem(withTitle: "Default Text Size", action: #selector(resetText(_:)), keyEquivalent: "0")
        view.addItem(.separator())
        view.addItem(item("Font", buildFontMenu()))
        let smoothing = view.addItem(withTitle: "Font Smoothing", action: #selector(toggleSmoothing(_:)), keyEquivalent: "")
        smoothing.state = fontSmoothing ? .on : .off
        bar.addItem(item("View", view))

        let monitor = NSMenu(title: "Monitor")
        monitor.addItem(withTitle: "Restart Monitor", action: #selector(restartCore(_:)), keyEquivalent: "r")
        monitor.addItem(.separator())
        monitor.addItem(withTitle: "Open Log Folder", action: #selector(openLogs(_:)), keyEquivalent: "")
        monitor.addItem(withTitle: "Reveal Settings File", action: #selector(revealSettings(_:)), keyEquivalent: "")
        bar.addItem(item("Monitor", monitor))

        let win = NSMenu(title: "Window")
        win.addItem(withTitle: "Minimize", action: #selector(NSWindow.miniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "Zoom", action: #selector(NSWindow.zoom(_:)), keyEquivalent: "")
        bar.addItem(item("Window", win))
        NSApp.windowsMenu = win

        let help = NSMenu(title: "Help")
        help.addItem(withTitle: "agent-farm on GitHub", action: #selector(openGitHub(_:)), keyEquivalent: "")
        bar.addItem(item("Help", help))
        NSApp.helpMenu = help

        NSApp.mainMenu = bar
    }

    private func item(_ title: String, _ menu: NSMenu) -> NSMenuItem {
        let i = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        i.submenu = menu
        return i
    }

    /// Every fixed-pitch family installed on this Mac, the current one checked.
    private func buildFontMenu() -> NSMenu {
        let menu = NSMenu(title: "Font")
        let manager = NSFontManager.shared
        let current = currentFont.familyName
        for family in manager.availableFontFamilies.sorted() {
            guard let f = manager.font(withFamily: family, traits: [], weight: 5, size: 13), f.isFixedPitch else { continue }
            let entry = NSMenuItem(title: family, action: #selector(chooseFont(_:)), keyEquivalent: "")
            entry.representedObject = f.fontName
            entry.state = family == current ? .on : .off
            menu.addItem(entry)
        }
        fontMenu = menu
        return menu
    }

    @objc func chooseFont(_ sender: NSMenuItem) {
        guard let name = sender.representedObject as? String else { return }
        fontName = name
        terminal.font = currentFont
        fontMenu?.items.forEach { $0.state = $0 === sender ? .on : .off }
    }

    @objc func toggleSmoothing(_ sender: NSMenuItem) {
        fontSmoothing.toggle()
        terminal.fontSmoothing = fontSmoothing
        terminal.needsDisplay = true
        sender.state = fontSmoothing ? .on : .off
    }

    @objc func biggerText(_ sender: Any?) { setFontSize(fontSize + 1) }
    @objc func smallerText(_ sender: Any?) { setFontSize(max(8, fontSize - 1)) }
    @objc func resetText(_ sender: Any?) { setFontSize(Theme.defaultFontSize) }

    private func setFontSize(_ size: CGFloat) {
        fontSize = size
        terminal.font = currentFont   // SwiftTerm resizes the pty; Textual reflows
    }

    @objc func openLogs(_ sender: Any?) {
        try? FileManager.default.createDirectory(at: Self.logDir, withIntermediateDirectories: true)
        NSWorkspace.shared.open(Self.logDir)
    }

    @objc func revealSettings(_ sender: Any?) {
        try? FileManager.default.createDirectory(at: Self.dataDir, withIntermediateDirectories: true)
        let file = Self.dataDir.appendingPathComponent("monitor_settings.json")
        if FileManager.default.fileExists(atPath: file.path) {
            NSWorkspace.shared.activateFileViewerSelecting([file])
        } else {
            NSWorkspace.shared.open(Self.dataDir)
        }
    }

    @objc func openGitHub(_ sender: Any?) {
        NSWorkspace.shared.open(URL(string: "https://github.com/djp3/agent-farm")!)
    }
}
