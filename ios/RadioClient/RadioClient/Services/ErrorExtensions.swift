import Foundation

extension Error {
    /// True if this error is the result of the hosting Task being cancelled (e.g. a
    /// newer sync/download superseded this one) rather than a genuine network/server
    /// failure. Cancellation is normal, expected behavior — not worth logging as an
    /// error — so every network call site should check this before logging a failure.
    var isCancellation: Bool {
        if self is CancellationError { return true }
        if let urlError = self as? URLError, urlError.code == .cancelled { return true }
        // Fallback: some cancellations arrive as a plain NSError rather than bridging
        // cleanly to URLError — check the raw domain/code too so none slip through.
        let nsError = self as NSError
        if nsError.domain == NSURLErrorDomain && nsError.code == NSURLErrorCancelled { return true }
        return false
    }
}
