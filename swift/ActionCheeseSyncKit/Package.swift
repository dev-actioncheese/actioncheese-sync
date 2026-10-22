// swift-tools-version: 5.9
// SPDX-License-Identifier: MIT
import PackageDescription

let package = Package(
    name: "ActionCheeseSyncKit",
    platforms: [.macOS(.v14), .iOS(.v17)],
    products: [
        .library(name: "ActionCheeseSyncKit", targets: ["ActionCheeseSyncKit"]),
    ],
    // No third-party dependencies on purpose: this ships inside an App Store
    // app, and AVFoundation plus Accelerate cover decoding and the FFTs.
    targets: [
        .target(name: "ActionCheeseSyncKit"),
        .testTarget(
            name: "ActionCheeseSyncKitTests",
            dependencies: ["ActionCheeseSyncKit"],
            resources: [.copy("Golden")]
        ),
    ]
)
