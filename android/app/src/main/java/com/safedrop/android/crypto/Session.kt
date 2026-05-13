package com.safedrop.android.crypto

import java.util.Base64
import org.bouncycastle.crypto.agreement.X25519Agreement
import org.bouncycastle.crypto.params.X25519PublicKeyParameters

/**
 * Symmetric encryption session derived from an X25519 ECDH exchange.
 * Mirrors the Python `crypto.derive_session` so both ends produce the
 * same Fernet key + 4-digit pair code from the same shared secret.
 */
class Session(private val fernet: Fernet, val pairCode: String) {
    fun encrypt(plaintext: ByteArray): ByteArray = fernet.encrypt(plaintext)
    fun decrypt(ciphertext: ByteArray): ByteArray = fernet.decrypt(ciphertext)
}

private val INFO_KEY = "SafeDrop v1 fernet key".toByteArray(Charsets.US_ASCII)
private val INFO_PAIR = "SafeDrop v1 pair code".toByteArray(Charsets.US_ASCII)

fun deriveSession(identity: Identity, peerPublicKeyBase64: String): Session {
    val peerBytes = Base64.getDecoder().decode(peerPublicKeyBase64)
    require(peerBytes.size == 32) { "peer pubkey must be 32 bytes, got ${peerBytes.size}" }
    val peerPub = X25519PublicKeyParameters(peerBytes, 0)

    val agreement = X25519Agreement().apply { init(identity.priv) }
    val shared = ByteArray(agreement.agreementSize)
    agreement.calculateAgreement(peerPub, shared, 0)

    val keyBytes = Hkdf.derive(shared, INFO_KEY, length = 32)
    val pairBytes = Hkdf.derive(shared, INFO_PAIR, length = 4)

    // big-endian unsigned u32 mod 10000, matches Python struct.unpack(">I", ...) % 10000.
    val pairLong =
        ((pairBytes[0].toLong() and 0xFF) shl 24) or
        ((pairBytes[1].toLong() and 0xFF) shl 16) or
        ((pairBytes[2].toLong() and 0xFF) shl 8) or
         (pairBytes[3].toLong() and 0xFF)
    val pairCode = "%04d".format(pairLong % 10000)

    return Session(Fernet(keyBytes), pairCode)
}
