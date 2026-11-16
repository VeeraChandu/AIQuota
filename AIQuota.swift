import Cocoa
import Foundation

struct LimitResponse: Codable {
    struct Tier: Codable {
        let name: String?
        let id: String?
    }
    struct Bucket: Codable {
        let id: String?
        let name: String?
        let window: String?
        let remainingFraction: Double?
        let resetTime: String?
        let resetsIn: String?
        let disabled: Bool?
        let description: String?
    }
    struct Group: Codable {
        let name: String?
        let description: String?
        let buckets: [Bucket]?
    }

    let email: String?
    let tier: Tier?
    let availableCredits: Int?
    let groups: [Group]?
}

class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    var timer: Timer?
    // Which providers the bar shows; persisted across launches.
    enum Mode: String, CaseIterable {
        case antigravity = "Antigravity", claude = "Claude Code", both = "Both"
    }
    var mode: Mode {
        get { Mode(rawValue: UserDefaults.standard.string(forKey: "mode") ?? "") ?? .antigravity }
        set { UserDefaults.standard.set(newValue.rawValue, forKey: "mode") }
    }
    var lastAG: LimitResponse?
    var lastCC: LimitResponse?
    var pythonScriptPath: String {
        if let bundled = Bundle.main.path(forResource: "aiquota", ofType: "py"),
           FileManager.default.fileExists(atPath: bundled) {
            return bundled
        }
        return NSString(string: "~/.local/bin/aiquota").expandingTildeInPath
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        
        setupIcon()
        
        if let button = statusItem.button {
            button.title = " Limits: ..."
        }
        
        buildInitialMenu()
        refreshLimits()
        
        // Auto refresh every 5 minutes (300 seconds)
        timer = Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { [weak self] _ in
            self?.refreshLimits()
        }
    }

    func setupIcon() {
        guard let button = statusItem.button else { return }
        
        // ponytail: SF Symbol instead of bundled PNGs — provider-neutral, auto Retina/dark mode
        let img = NSImage(systemSymbolName: "gauge.with.dots.needle.67percent", accessibilityDescription: "AIQuota")
            ?? NSImage(systemSymbolName: "speedometer", accessibilityDescription: "AIQuota")
        img?.isTemplate = true
        button.image = img
        button.imagePosition = .imageLeft
    }

    func buildInitialMenu() {
        let menu = NSMenu()
        let titleItem = NSMenuItem(title: "AIQuota", action: nil, keyEquivalent: "")
        titleItem.isEnabled = false
        menu.addItem(titleItem)
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "Fetching quota...", action: nil, keyEquivalent: ""))
        menu.addItem(NSMenuItem.separator())
        
        let refreshItem = NSMenuItem(title: "Refresh Now", action: #selector(refreshClicked), keyEquivalent: "r")
        refreshItem.target = self
        menu.addItem(refreshItem)
        
        let quitItem = NSMenuItem(title: "Quit", action: #selector(quitClicked), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)
        
        statusItem.menu = menu
    }

    @objc func refreshClicked() {
        if let button = statusItem.button {
            button.title = " Fetching..."
        }
        refreshLimits()
    }

    @objc func quitClicked() {
        NSApp.terminate(nil)
    }

    func fetch(_ extraArg: String?) -> LimitResponse? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = [pythonScriptPath, "--json"] + (extraArg.map { [$0] } ?? [])
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = Pipe()
        guard (try? process.run()) != nil else { return nil }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return try? JSONDecoder().decode(LimitResponse.self, from: data)
    }

    func refreshLimits() {
        let mode = self.mode
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self = self else { return }
            let ag = mode != .claude ? self.fetch(nil) : nil
            let cc = mode != .antigravity ? self.fetch("--claude") : nil
            DispatchQueue.main.async {
                self.lastAG = ag
                self.lastCC = cc
                self.updateUI()
            }
        }
    }

    @objc func modeClicked(_ sender: NSMenuItem) {
        guard let m = Mode(rawValue: sender.title) else { return }
        mode = m
        statusItem.button?.title = " Fetching..."
        refreshLimits()
    }

    func pct(_ b: LimitResponse.Bucket) -> String {
        "\(Int(round((b.remainingFraction ?? 0) * 100)))% (\(b.resetsIn ?? "N/A"))"
    }

    func updateUI() {
        let mode = self.mode
        var parts: [String] = []

        if mode != .claude {
            var gShort = "N/A", cShort = "N/A"
            for g in lastAG?.groups ?? [] {
                let name = (g.name ?? "").lowercased()
                if name.contains("gemini"), let b = g.buckets?.first(where: { $0.window == "5h" }) {
                    gShort = pct(b)
                } else if name.contains("claude") || name.contains("gpt"),
                          let b = g.buckets?.first(where: { $0.window == "weekly" }) {
                    cShort = pct(b)
                }
            }
            parts.append(mode == .both ? "G: \(gShort)" : "G: \(gShort) | C: \(cShort)")
        }
        if mode != .antigravity {
            let bs = lastCC?.groups?.first?.buckets ?? []
            let five = bs.first(where: { $0.window == "5h" }).map(pct) ?? "N/A"
            let week = bs.first(where: { $0.window == "weekly" }).map { "\(Int(round(($0.remainingFraction ?? 0) * 100)))%" } ?? "N/A"
            parts.append("CC: \(five) | W: \(week)")
        }
        statusItem.button?.title = " " + parts.joined(separator: " | ")

        // Build Dropdown Menu
        let menu = NSMenu()
        func add(_ title: String) {
            let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            item.isEnabled = false
            menu.addItem(item)
        }

        let sections: [(String, LimitResponse?, Bool)] = [
            ("Antigravity", lastAG, mode != .claude),
            ("Claude Code", lastCC, mode != .antigravity),
        ]
        for (label, res, shown) in sections where shown {
            guard let res = res else {
                add("⚠️ \(label): fetch failed")
                menu.addItem(NSMenuItem.separator())
                continue
            }
            if let email = res.email { add("👤 \(email)") }
            add("✦ \(label) Plan: \(res.tier?.name ?? "Unknown")")
            if label == "Antigravity" { add("💰 Available AI Credits: \(res.availableCredits ?? 0)") }
            menu.addItem(NSMenuItem.separator())

            for g in res.groups ?? [] {
                add(g.name ?? "Quota Group")
                for b in g.buckets ?? [] {
                    let p = Int(round((b.remainingFraction ?? 0) * 100))
                    let tail = (b.disabled ?? false) ? "Paused" : "Resets in \(b.resetsIn ?? "N/A")"
                    add("   • \(b.name ?? ""): \(p)% (\(tail))")
                }
                menu.addItem(NSMenuItem.separator())
            }
        }

        // Provider switch
        let showItem = NSMenuItem(title: "Show", action: nil, keyEquivalent: "")
        let sub = NSMenu()
        for m in Mode.allCases {
            let item = NSMenuItem(title: m.rawValue, action: #selector(modeClicked(_:)), keyEquivalent: "")
            item.target = self
            item.state = m == mode ? .on : .off
            sub.addItem(item)
        }
        showItem.submenu = sub
        menu.addItem(showItem)
        menu.addItem(NSMenuItem.separator())

        let refreshItem = NSMenuItem(title: "Refresh Now", action: #selector(refreshClicked), keyEquivalent: "r")
        refreshItem.target = self
        menu.addItem(refreshItem)

        let quitItem = NSMenuItem(title: "Quit", action: #selector(quitClicked), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)

        statusItem.menu = menu
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
