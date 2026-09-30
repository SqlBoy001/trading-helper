import Foundation
import Vision
import AppKit
let url = URL(fileURLWithPath: CommandLine.arguments[1])
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["zh-Hans", "en-US"]
request.usesLanguageCorrection = false
let handler = VNImageRequestHandler(url: url)
do {
    try handler.perform([request])
    let rows = (request.results ?? []).compactMap { row -> [String: Any]? in
        guard let candidate = row.topCandidates(1).first else { return nil }
        return ["text": candidate.string, "confidence": candidate.confidence]
    }
    let data = try JSONSerialization.data(withJSONObject: rows)
    print(String(data: data, encoding: .utf8)!)
} catch {
    fputs("Local OCR could not read image\n", stderr)
    exit(1)
}
