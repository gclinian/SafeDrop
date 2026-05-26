import Combine
import Foundation
import SwiftUI
import UIKit

/// One in-flight take_photo request. The tool handler (which runs on
/// SafeDrop's ioQueue) constructs this, pushes it onto the broker, and
/// blocks on the semaphore until the UI either delivers a JPEG or cancels.
final class PhotoCaptureRequest: ObservableObject, Identifiable {
    let id = UUID()
    let peerName: String
    let pairCode: String
    private let sema = DispatchSemaphore(value: 0)
    private(set) var resultData: Data?
    private(set) var errorMessage: String?

    init(peerName: String, pairCode: String) {
        self.peerName = peerName
        self.pairCode = pairCode
    }

    /// Called from the SwiftUI side after the user shutters or cancels.
    func deliver(data: Data?) {
        self.resultData = data
        sema.signal()
    }

    func cancel(_ reason: String) {
        self.errorMessage = reason
        sema.signal()
    }

    /// Block this thread until a delivery / cancel arrives. ``MUST`` be
    /// called off the main thread.
    func waitSync(timeout: TimeInterval) -> (Data?, String?) {
        if sema.wait(timeout: .now() + timeout) == .timedOut {
            return (nil, "timeout waiting for camera")
        }
        return (resultData, errorMessage)
    }
}

/// Service-level broker — one per app. The take_photo tool calls
/// :method:`capture` (sync, blocking); SwiftUI observes :attr:`pending`
/// and presents the camera.
final class PhotoBroker: ObservableObject {
    @Published var pending: PhotoCaptureRequest?

    /// Blocking. Returns ``(jpegData, errorMessage)`` — at most one is non-nil.
    func capture(peerName: String, pairCode: String, timeout: TimeInterval = 120) -> (Data?, String?) {
        let req = PhotoCaptureRequest(peerName: peerName, pairCode: pairCode)
        DispatchQueue.main.async { [weak self] in
            self?.pending = req
        }
        let (data, err) = req.waitSync(timeout: timeout)
        DispatchQueue.main.async { [weak self] in
            if self?.pending?.id == req.id {
                self?.pending = nil
            }
        }
        return (data, err)
    }
}

// MARK: - SwiftUI camera presentation

/// UIImagePickerController wrapped for SwiftUI. The .source(.camera)
/// flow shows the system shutter; on confirm we encode to JPEG and
/// deliver bytes to the request.
struct CameraView: UIViewControllerRepresentable {
    @ObservedObject var request: PhotoCaptureRequest
    @Environment(\.dismiss) private var dismiss

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeUIViewController(context: Context) -> UIImagePickerController {
        let picker = UIImagePickerController()
        // Fall back to the photo library if the device doesn't have a
        // camera (e.g. simulators with simulated camera disabled). The
        // tool description doesn't promise live camera — just "image".
        if UIImagePickerController.isSourceTypeAvailable(.camera) {
            picker.sourceType = .camera
            picker.cameraCaptureMode = .photo
        } else {
            picker.sourceType = .photoLibrary
        }
        picker.delegate = context.coordinator
        picker.allowsEditing = false
        return picker
    }

    func updateUIViewController(_ uiViewController: UIImagePickerController, context: Context) {}

    final class Coordinator: NSObject, UIImagePickerControllerDelegate, UINavigationControllerDelegate {
        let parent: CameraView
        init(_ parent: CameraView) { self.parent = parent }

        func imagePickerController(_ picker: UIImagePickerController,
                                   didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]) {
            let image = (info[.originalImage] as? UIImage)
            // Resize to a sensible cap so a 12 MP shot doesn't push 5 MB
            // through the SafeDrop frame. 1600 px on the long edge keeps
            // useful detail without bloating the encrypted payload.
            let resized = image.map { resize($0, longEdge: 1600) } ?? nil
            let jpeg = resized?.jpegData(compressionQuality: 0.82)
            parent.request.deliver(data: jpeg)
            parent.dismiss()
        }

        func imagePickerControllerDidCancel(_ picker: UIImagePickerController) {
            parent.request.cancel("user cancelled")
            parent.dismiss()
        }

        private func resize(_ image: UIImage, longEdge: CGFloat) -> UIImage {
            let w = image.size.width, h = image.size.height
            let longest = max(w, h)
            if longest <= longEdge { return image }
            let scale = longEdge / longest
            let newSize = CGSize(width: w * scale, height: h * scale)
            let format = UIGraphicsImageRendererFormat.default()
            format.scale = 1
            return UIGraphicsImageRenderer(size: newSize, format: format).image { _ in
                image.draw(in: CGRect(origin: .zero, size: newSize))
            }
        }
    }
}
