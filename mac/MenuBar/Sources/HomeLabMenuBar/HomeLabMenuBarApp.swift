import SwiftUI

@main
struct HomeLabMenuBarApp: App {
    @StateObject private var store = ProfileStore()

    var body: some Scene {
        MenuBarExtra {
            MenuView().environmentObject(store)
        } label: {
            Image(systemName: Self.symbol(store.status))
        }
        .menuBarExtraStyle(.window)
    }

    static func symbol(_ status: ProfileStatus?) -> String {
        switch (status?.profile, status?.paused) {
        case ("working", _): "laptopcomputer"
        case ("inference", true): "pause.circle"
        case ("inference", _): "bolt.circle"
        case ("custom", _): "slider.horizontal.3"
        default: "questionmark.circle"
        }
    }
}
