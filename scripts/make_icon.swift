// Draws the agent-farm app icon, the rooster emoji on the app's dark rounded square, and
// writes an .icns.   usage: swift scripts/make_icon.swift <rooster image> <out.icns>
// The rooster is Noto Emoji's 3D U+1F413 (3D/png/512/emoji_u1f413.png, Google LLC); see
// macos/Resources/NOTICE-noto-emoji.txt. Apple's own emoji artwork is not licensed for
// redistribution, so it must not be used here. The art is 512 px, so the 1024 px slot is
// upscaled with high-quality interpolation. Any image NSImage reads works, SVG included.
import AppKit
import Foundation

func color(_ hex: String) -> NSColor {
    var h = hex.hasPrefix("#") ? String(hex.dropFirst()) : hex
    if h.count == 3 { h = h.map { "\($0)\($0)" }.joined() }
    let v = UInt32(h, radix: 16) ?? 0
    return NSColor(srgbRed: CGFloat((v >> 16) & 0xff) / 255, green: CGFloat((v >> 8) & 0xff) / 255,
                   blue: CGFloat(v & 0xff) / 255, alpha: 1)
}

func render(_ art: NSImage, pixels: Int) -> Data {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels, bitsPerSample: 8,
                               samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                               colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    let ctx = NSGraphicsContext(bitmapImageRep: rep)!
    ctx.imageInterpolation = .high
    NSGraphicsContext.current = ctx
    let s = CGFloat(pixels)
    let inset = s * 0.09                            // Apple's icon grid leaves a margin
    let r = NSRect(x: inset, y: inset, width: s - 2 * inset, height: s - 2 * inset)
    let radius = r.width * 0.2237
    color("#15171c").setFill()
    NSBezierPath(roundedRect: r, xRadius: radius, yRadius: radius).fill()

    let side = r.width * 0.80                       // the rooster's square, centered in the tile
    art.draw(in: NSRect(x: r.midX - side / 2, y: r.midY - side / 2, width: side, height: side),
             from: .zero, operation: .sourceOver, fraction: 1)
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

let args = CommandLine.arguments
guard args.count == 3, let art = NSImage(contentsOf: URL(fileURLWithPath: args[1])) else {
    FileHandle.standardError.write("usage: swift scripts/make_icon.swift <rooster image> <out.icns>\n".data(using: .utf8)!)
    exit(2)
}
let out = args[2]
let tmp = FileManager.default.temporaryDirectory.appendingPathComponent("agent-farm-\(getpid()).iconset")
try FileManager.default.createDirectory(at: tmp, withIntermediateDirectories: true)
for base in [16, 32, 128, 256, 512] {
    try render(art, pixels: base).write(to: tmp.appendingPathComponent("icon_\(base)x\(base).png"))
    try render(art, pixels: base * 2).write(to: tmp.appendingPathComponent("icon_\(base)x\(base)@2x.png"))
}
let p = Process()
p.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
p.arguments = ["-c", "icns", tmp.path, "-o", out]
try p.run(); p.waitUntilExit()
try? FileManager.default.removeItem(at: tmp)
if p.terminationStatus != 0 { exit(p.terminationStatus) }
print("wrote \(out)")
