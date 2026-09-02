import Foundation

enum CacheFormat {
    static func bytes(_ bytes: Int64) -> String {
        let mb = Double(bytes) / (1024 * 1024)
        if mb >= 1024 {
            return String(format: "%.1f GB", mb / 1024)
        }
        return String(format: "%.0f MB", mb)
    }

    static func duration(_ seconds: Double) -> String {
        // Int(Double) traps on NaN/infinity — never let a bad duration crash a render.
        guard seconds.isFinite, seconds >= 0 else { return "0:00" }
        let totalMinutes = Int(min(seconds, 1e9).rounded()) / 60
        let hours = totalMinutes / 60
        let minutes = totalMinutes % 60
        return "\(hours):\(String(format: "%02d", minutes))"
    }
}
