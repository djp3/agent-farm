// Copyright (C) 2026 Don Patterson
// SPDX-License-Identifier: GPL-3.0-or-later
import AppKit

/// A label that is not there for the mouse: a press or a drag on it goes to the title bar
/// beneath, so the window is moved, and zoomed by a double click, there as anywhere on the bar.
private final class TitleLabel: NSTextField {
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
}

/// The window's title, in the middle of its title bar.
///
/// macOS puts a window's title at the leading edge of the bar, beside the buttons, and has
/// no setting for where it goes. So the system's own title is hidden, and this label stands
/// in for it: the title bar's font and color, centered in the bar, cut short with an
/// ellipsis before it would reach the buttons. That color is the system's for a title, and
/// dims by itself while the window is not the one in use, as the buttons beside it do.
///
/// The window keeps its title all the same. That is what the Dock, Mission Control, the
/// Window menu and VoiceOver go by, and it is the one copy: the label watches it and draws
/// what it says, so whoever sets the window's title has set what the bar shows.
@MainActor
final class CenteredTitle {
    private let label = TitleLabel(labelWithString: "")
    private var titled: NSKeyValueObservation?

    init(in window: NSWindow) {
        // The bar is where the buttons are, and it runs the width of the window. Where that
        // is not so (no such window is made here, but macOS decides how a title bar is put
        // together) there is nowhere to center a title in, and the system's stays as it is.
        guard let bar = window.standardWindowButton(.closeButton)?.superview,
              let frame = window.contentView?.superview, abs(bar.frame.width - frame.frame.width) < 1 else { return }
        window.titleVisibility = .hidden
        label.font = .titleBarFont(ofSize: 0)
        label.textColor = .windowFrameTextColor
        label.alignment = .center
        label.lineBreakMode = .byTruncatingTail
        label.maximumNumberOfLines = 1                // as the system's title is: see where it is drawn
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        // Hiding the system's title leaves the window with nothing that shows its title, for
        // what finds things under the pointer (Hover Text, VoiceOver following the mouse).
        // The label is that now.
        window.setAccessibilityTitleUIElement(label)
        label.translatesAutoresizingMaskIntoConstraints = false
        bar.addSubview(label)
        // Nothing that holds the label is as strong as what holds the window to its size
        // (.windowSizeStayPut). Where it cannot all hold (a window narrower than its buttons
        // and a few letters) the label is what gives way: first its width, then being in the
        // middle. A stronger rule would be kept by stretching the system's title bar
        // instead, wider than the window it is in.
        let middle = [label.centerXAnchor.constraint(equalTo: bar.centerXAnchor),
                      label.centerYAnchor.constraint(equalTo: bar.centerYAnchor)]
        middle.forEach { $0.priority = NSLayoutConstraint.Priority(480) }
        var held = middle
        // In the middle, so what keeps it off the buttons on one side keeps it as far from
        // the other edge. Which side the buttons are on is the title bar's to say, not the
        // app's: on a Mac set to a right-to-left language they are on the right, in an app
        // like this one that is laid out left to right all the same. So the side is asked
        // of the window and said in left and right, which do not turn with the app.
        if let last = window.standardWindowButton(.zoomButton) {
            let clear = window.windowTitlebarLayoutDirection == .rightToLeft
                ? label.rightAnchor.constraint(lessThanOrEqualTo: last.leftAnchor, constant: -8)
                : label.leftAnchor.constraint(greaterThanOrEqualTo: last.rightAnchor, constant: 8)
            clear.priority = .dragThatCannotResizeWindow         // 490: before being in the middle
            held.append(clear)
        }
        NSLayoutConstraint.activate(held)
        titled = window.observe(\.title, options: [.initial, .new]) { [label] window, _ in
            // A window's title is set on the main thread, and that is where this is told.
            MainActor.assumeIsolated {
                // One line, as the system's title is. Being told to keep to one line does
                // not stop a label breaking at a line or paragraph separator, so those are
                // drawn as spaces.
                label.stringValue = window.title.components(separatedBy: .newlines).joined(separator: " ")
            }
        }
    }
}
