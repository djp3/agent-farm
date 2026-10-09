// swift-tools-version:5.9
// The native shell of agent-farm: a window hosting a SwiftTerm terminal that runs the
// PyInstaller-built Python TUI (agent-farm-core), plus Sparkle for updates.
// scripts/build_app.sh builds this and assembles dist/agent-farm.app around it.
import PackageDescription

let package = Package(
    name: "AgentFarm",
    platforms: [.macOS(.v13)],
    dependencies: [
        .package(url: "https://github.com/migueldeicaza/SwiftTerm.git", from: "1.99.0"),
        .package(url: "https://github.com/sparkle-project/Sparkle.git", from: "2.10.0"),
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
