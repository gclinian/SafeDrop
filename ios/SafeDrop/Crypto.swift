import CommonCrypto
import CryptoKit
import Foundation

// MARK: - Identity (X25519 keypair)

/// Process-lifetime X25519 keypair. Public key is base64-exchanged in
/// discovery HELLO + TCP handshake. Mirrors Python's `safedrop/crypto.py`
/// and Android's `Identity.kt` byte-for-byte.
final class Identity {
    let privateKey: Curve25519.KeyAgreement.PrivateKey

    init() { self.privateKey = Curve25519.KeyAgreement.PrivateKey() }
    init(privateKey: Curve25519.KeyAgreement.PrivateKey) { self.privateKey = privateKey }

    var publicKey: Curve25519.KeyAgreement.PublicKey { privateKey.publicKey }

    func publicKeyBase64() -> String {
        publicKey.rawRepresentation.base64EncodedString()
    }
}

// MARK: - HKDF (matches Python / Kotlin: salt=None → 32 zero bytes)

enum Hkdf {
    static func derive(ikm: Data, info: Data, length: Int, salt: Data? = nil) -> Data {
        let effectiveSalt = salt ?? Data(count: 32)
        let prk = HMAC<SHA256>.authenticationCode(for: ikm, using: SymmetricKey(data: effectiveSalt))
        let prkData = Data(prk)

        var okm = Data()
        var previous = Data()
        var counter: UInt8 = 1
        while okm.count < length {
            var t = previous
            t.append(info)
            t.append(counter)
            let next = HMAC<SHA256>.authenticationCode(for: t, using: SymmetricKey(data: prkData))
            previous = Data(next)
            okm.append(previous)
            counter = counter &+ 1
        }
        return okm.prefix(length)
    }
}

// MARK: - Fernet

/// Minimal Fernet implementation matching Python's cryptography.fernet.
/// Token layout (before base64-url):
///   0x80 | timestamp(8 BE) | iv(16) | ciphertext | hmac-sha256(32)
/// Key bytes:
///   [0..16) → HMAC signing key
///   [16..32) → AES-128 key
struct Fernet {
    let signingKey: Data
    let encKey: Data

    init(key: Data) {
        precondition(key.count == 32, "Fernet key must be 32 bytes")
        self.signingKey = key.subdata(in: 0..<16)
        self.encKey = key.subdata(in: 16..<32)
    }

    func encrypt(_ plaintext: Data) throws -> Data {
        var iv = Data(count: 16)
        let ivStatus = iv.withUnsafeMutableBytes { SecRandomCopyBytes(kSecRandomDefault, 16, $0.baseAddress!) }
        guard ivStatus == errSecSuccess else { throw FernetError.random }
        let timestamp = UInt64(Date().timeIntervalSince1970)
        let ciphertext = try Self.aesCbc(.encrypt, key: encKey, iv: iv, data: plaintext)

        var body = Data()
        body.append(0x80)
        var be = timestamp.bigEndian
        body.append(Data(bytes: &be, count: 8))
        body.append(iv)
        body.append(ciphertext)

        let mac = HMAC<SHA256>.authenticationCode(for: body, using: SymmetricKey(data: signingKey))
        var token = body
        token.append(contentsOf: mac)
        return Self.base64Url(token)
    }

    func decrypt(_ token: Data) throws -> Data {
        let data = try Self.base64UrlDecode(token)
        guard data.count >= 1 + 8 + 16 + 32 + 16 else { throw FernetError.shortToken }
        guard data[0] == 0x80 else { throw FernetError.badVersion(data[0]) }

        let hmacOffset = data.count - 32
        let body = data.subdata(in: 0..<hmacOffset)
        let tag = data.subdata(in: hmacOffset..<data.count)
        let expected = HMAC<SHA256>.authenticationCode(for: body, using: SymmetricKey(data: signingKey))
        guard tag.elementsEqual(expected) else { throw FernetError.hmacMismatch }

        let iv = data.subdata(in: 9..<25)
        let ct = data.subdata(in: 25..<hmacOffset)
        return try Self.aesCbc(.decrypt, key: encKey, iv: iv, data: ct)
    }

    enum Op { case encrypt, decrypt }
    enum FernetError: Error { case shortToken, badVersion(UInt8), hmacMismatch, crypto(Int32), random }

    private static func aesCbc(_ op: Op, key: Data, iv: Data, data: Data) throws -> Data {
        precondition(key.count == 16 && iv.count == 16, "bad key/iv length")
        let bufCapacity = data.count + kCCBlockSizeAES128
        var out = Data(count: bufCapacity)
        var written = 0
        let dataLen = data.count
        let status: CCCryptorStatus = out.withUnsafeMutableBytes { outBuf in
            data.withUnsafeBytes { dataBuf in
                iv.withUnsafeBytes { ivBuf in
                    key.withUnsafeBytes { keyBuf in
                        CCCrypt(
                            CCOperation(op == .encrypt ? kCCEncrypt : kCCDecrypt),
                            CCAlgorithm(kCCAlgorithmAES),
                            CCOptions(kCCOptionPKCS7Padding),
                            keyBuf.baseAddress, 16,
                            ivBuf.baseAddress,
                            dataBuf.baseAddress, dataLen,
                            outBuf.baseAddress, bufCapacity,
                            &written
                        )
                    }
                }
            }
        }
        guard status == kCCSuccess else { throw FernetError.crypto(status) }
        out.removeSubrange(written..<out.count)
        return out
    }

    // ---- URL-safe base64 with padding (Python's urlsafe_b64encode/decode)
    private static func base64Url(_ data: Data) -> Data {
        // Standard base64 → translate to URL-safe.
        let std = data.base64EncodedString()
        let url = std.replacingOccurrences(of: "+", with: "-")
                     .replacingOccurrences(of: "/", with: "_")
        return Data(url.utf8)
    }

    private static func base64UrlDecode(_ data: Data) throws -> Data {
        guard var s = String(data: data, encoding: .utf8) else { throw FernetError.shortToken }
        s = s.replacingOccurrences(of: "-", with: "+")
             .replacingOccurrences(of: "_", with: "/")
        // Add padding if missing
        let rem = s.count % 4
        if rem > 0 { s += String(repeating: "=", count: 4 - rem) }
        guard let d = Data(base64Encoded: s) else { throw FernetError.shortToken }
        return d
    }
}

// MARK: - Session (Fernet + 4-digit pair code derived from X25519 ECDH)

private let kInfoFernet = Data("SafeDrop v1 fernet key".utf8)
private let kInfoPair   = Data("SafeDrop v1 pair code".utf8)

struct Session {
    let fernet: Fernet
    let pairCode: String

    func encrypt(_ plaintext: Data) throws -> Data { try fernet.encrypt(plaintext) }
    func decrypt(_ ciphertext: Data) throws -> Data { try fernet.decrypt(ciphertext) }
}

func deriveSession(identity: Identity, peerPublicKeyBase64: String) throws -> Session {
    guard let rawData = Data(base64Encoded: peerPublicKeyBase64), rawData.count == 32 else {
        throw NSError(domain: "SafeDrop", code: 1,
                      userInfo: [NSLocalizedDescriptionKey: "peer pubkey must be 32 raw bytes"])
    }
    let peerPub = try Curve25519.KeyAgreement.PublicKey(rawRepresentation: rawData)
    let shared = try identity.privateKey.sharedSecretFromKeyAgreement(with: peerPub)
    let sharedData = shared.withUnsafeBytes { Data($0) }

    let keyBytes = Hkdf.derive(ikm: sharedData, info: kInfoFernet, length: 32)
    let pairBytes = Hkdf.derive(ikm: sharedData, info: kInfoPair, length: 4)

    let bigEndianU32 = UInt32(pairBytes[0]) << 24
                     | UInt32(pairBytes[1]) << 16
                     | UInt32(pairBytes[2]) << 8
                     | UInt32(pairBytes[3])
    let pairCode = String(format: "%04d", bigEndianU32 % 10000)
    return Session(fernet: Fernet(key: keyBytes), pairCode: pairCode)
}
