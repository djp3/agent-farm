// Draws the agent-farm app icon with the TUI's own glyph language (a filled "has
// children" triangle followed by three agent circles in the busy / idle / waiting
// colors) and writes an .icns.   usage: swift scripts/make_icon.swift <out.icns>
import AppKit
import Foundation

func color(_ hex: String) -> NSColor {
    var h = hex.hasPrefix("#") ? String(hex.dropFirst()) : hex
    if h.count == 3 { h = h.map { "\($0)\($0)" }.joined() }
    let v = UInt32(h, radix: 16) ?? 0
    return NSColor(srgbRed: CGFloat((v >> 16) & 0xff) / 255, green: CGFloat((v >> 8) & 0xff) / 255,
                   blue: CGFloat(v & 0xff) / 255, alpha: 1)
}

func render(pixels: Int) -> Data {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels, bitsPerSample: 8,
                               samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                               colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let s = CGFloat(pixels)
    let inset = s * 0.09                            // Apple's icon grid leaves a margin
    let r = NSRect(x: inset, y: inset, width: s - 2 * inset, height: s - 2 * inset)
    let radius = r.width * 0.2237
    color("#15171c").setFill()
    NSBezierPath(roundedRect: r, xRadius: radius, yRadius: radius).fill()

    let cy = r.midY
    let h = r.height * 0.40                         // triangle height
    let tri = NSBezierPath()
    let tx = r.minX + r.width * 0.16
    tri.move(to: NSPoint(x: tx, y: cy + h / 2))
    tri.line(to: NSPoint(x: tx, y: cy - h / 2))
    tri.line(to: NSPoint(x: tx + h * 0.95, y: cy))
    tri.close()
    color("#00d75f").setFill()
    tri.fill()

    let rad = r.width * 0.062
    for (i, c) in ["#00d75f", "#5f87ff", "#ff9500"].enumerated() {
        let x = r.minX + r.width * (0.62 + 0.135 * CGFloat(i))
        color(c).setFill()
        NSBezierPath(ovalIn: NSRect(x: x - rad, y: cy - rad, width: 2 * rad, height: 2 * rad)).fill()
    }
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "AppIcon.icns"
let tmp = FileManager.default.temporaryDirectory.appendingPathComponent("agent-farm-\(getpid()).iconset")
try FileManager.default.createDirectory(at: tmp, withIntermediateDirectories: true)
for base in [16, 32, 128, 256, 512] {
    try render(pixels: base).write(to: tmp.appendingPathComponent("icon_\(base)x\(base).png"))
    try render(pixels: base * 2).write(to: tmp.appendingPathComponent("icon_\(base)x\(base)@2x.png"))
}
let p = Process()
p.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
p.arguments = ["-c", "icns", tmp.path, "-o", out]
try p.run(); p.waitUntilExit()
try? FileManager.default.removeItem(at: tmp)
if p.terminationStatus != 0 { exit(p.terminationStatus) }
print("wrote \(out)")
