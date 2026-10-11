import SwiftUI
import AVFoundation

struct PairingScanner: UIViewControllerRepresentable {
    let onScan: (String) -> Void
    let onError: (String) -> Void
    func makeUIViewController(context: Context) -> ScannerController { ScannerController(onScan: onScan, onError: onError) }
    func updateUIViewController(_ controller: ScannerController, context: Context) {}
    static func dismantleUIViewController(_ controller: ScannerController, coordinator: ()) { controller.stop() }
}

final class ScannerController: UIViewController, AVCaptureMetadataOutputObjectsDelegate {
    let session = AVCaptureSession()
    let queue = DispatchQueue(label: "pudge.qr.camera")
    let onScan: (String) -> Void
    let onError: (String) -> Void
    var preview: AVCaptureVideoPreviewLayer?
    var scanned = false
    init(onScan: @escaping (String) -> Void, onError: @escaping (String) -> Void) {
        self.onScan = onScan; self.onError = onError; super.init(nibName: nil, bundle: nil)
    }
    required init?(coder: NSCoder) { fatalError("Use init(onScan:onError:)") }
    override func viewDidLoad() {
        super.viewDidLoad(); view.backgroundColor = .black
        AVCaptureDevice.requestAccess(for: .video) { [weak self] granted in
            guard let self else { return }
            if granted { self.configure() }
            else { DispatchQueue.main.async { self.onError("Camera access is denied. Paste the invitation from the Mac's settings.") } }
        }
    }
    func configure() {
        queue.async { [weak self] in
            guard let self, let camera = AVCaptureDevice.default(for: .video) else { return }
            do {
                let input = try AVCaptureDeviceInput(device: camera)
                let output = AVCaptureMetadataOutput()
                guard self.session.canAddInput(input), self.session.canAddOutput(output) else { return }
                self.session.addInput(input); self.session.addOutput(output)
                output.setMetadataObjectsDelegate(self, queue: .main); output.metadataObjectTypes = [.qr]
                DispatchQueue.main.async {
                    let layer = AVCaptureVideoPreviewLayer(session: self.session); layer.videoGravity = .resizeAspectFill
                    self.view.layer.addSublayer(layer); self.preview = layer; layer.frame = self.view.bounds
                }
                self.session.startRunning()
            } catch { DispatchQueue.main.async { self.onError(error.localizedDescription) } }
        }
    }
    override func viewDidLayoutSubviews() { super.viewDidLayoutSubviews(); preview?.frame = view.bounds }
    func metadataOutput(_ output: AVCaptureMetadataOutput, didOutput metadataObjects: [AVMetadataObject], from connection: AVCaptureConnection) {
        guard !scanned, let result = metadataObjects.first as? AVMetadataMachineReadableCodeObject, let value = result.stringValue else { return }
        scanned = true; stop(); onScan(value)
    }
    func stop() { queue.async { [weak self] in self?.session.stopRunning() } }
}
