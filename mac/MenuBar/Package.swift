// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "HomeLabMenuBar",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "HomeLabMenuBar", targets: ["HomeLabMenuBar"])],
    targets: [.executableTarget(name: "HomeLabMenuBar")]
)
