package com.safedrop.android.crypto

import java.security.SecureRandom
import java.util.Base64
import org.bouncycastle.crypto.params.X25519PrivateKeyParameters
import org.bouncycastle.crypto.params.X25519PublicKeyParameters

/**
 * Process-lifetime X25519 keypair. Public key is exchanged in discovery
 * HELLO and TCP handshake; the private key never leaves the process.
 */
class Identity private constructor(internal val priv: X25519PrivateKeyParameters) {

    val publicKey: X25519PublicKeyParameters get() = priv.generatePublicKey()

    fun publicKeyBytes(): ByteArray = publicKey.encoded

    fun publicKeyBase64(): String = Base64.getEncoder().encodeToString(publicKeyBytes())

    companion object {
        fun generate(): Identity = Identity(X25519PrivateKeyParameters(SecureRandom()))
    }
}
