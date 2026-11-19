// swift-tools-version:5.9
// The native shell of agent-farm: a window hosting a SwiftTerm terminal that runs the
// PyInstaller-built Python TUI (agent-farm-core), plus Sparkle for updates.
// scripts/build_app.sh builds this and assembles dist/agent-farm.app around it.
import PackageDescription

let package = Package(
    name: "AgentFarm",
    // macOS 27, the same minimum as LSMinimumSystemVersion in Info.plist: the bundled Python
    // (Homebrew's) is built for it. scripts/check_min_macos.py keeps the two honest.
    platforms: [.macOS("27.0")],
    dependencies: [
        .package(url: "https://github.com/migueldeicaza/SwiftTerm.git", from: "1.99.3"),
        .package(url: "https://github.com/sparkle-project/Sparkle.git", from: "2.10.1"),
    ],
    targets: [
        .executableTarget(
            name: "AgentFarm",
            dependencies: [
                .product(name: "SwiftTerm", package: "SwiftTerm"),
                .product(name: "Sparkle", package: "Sparkle"),
            ],
            path: "Sources/AgentFarm",
            linkerSettings: [
                // Sparkle.framework is copied into agent-farm.app/Contents/Frameworks by build_app.sh.
                .unsafeFlags(["-Xlinker", "-rpath", "-Xlinker", "@executable_path/../Frameworks"]),
            ]
        ),
    ]
)
