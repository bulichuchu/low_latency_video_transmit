import AVFoundation
import CoreMedia
import Foundation

// Enumerate metadata only. Do not create or start a capture session.
func fourCC(_ value: FourCharCode) -> String {
    let bytes = [24, 16, 8, 0].map { UInt8((value >> $0) & 255) }
    return String(bytes: bytes, encoding: .ascii) ?? String(value)
}

let devices = AVCaptureDevice.devices(for: .video)
let records: [[String: Any]] = devices.enumerated().map { index, device in
    let formats: [[String: Any]] = device.formats.map { format in
        let desc = format.formatDescription
        let size = CMVideoFormatDescriptionGetDimensions(desc)
        return [
            "width": Int(size.width), "height": Int(size.height),
            "media_subtype": fourCC(CMFormatDescriptionGetMediaSubType(desc)),
            "frame_rates": format.videoSupportedFrameRateRanges.map {
                ["min": $0.minFrameRate, "max": $0.maxFrameRate]
            }
        ]
    }
    return ["index_hint": index, "name": device.localizedName,
            "unique_id": device.uniqueID, "model_id": device.modelID,
            "formats": formats]
}
let output: [String: Any] = ["backend": "avfoundation", "metadata_only": true,
    "authorization_status": AVCaptureDevice.authorizationStatus(for: .video).rawValue,
    "devices": records]
let data = try JSONSerialization.data(withJSONObject: output, options: [.prettyPrinted, .sortedKeys])
FileHandle.standardOutput.write(data)
print("")
