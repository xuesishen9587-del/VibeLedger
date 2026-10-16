package com.vibeledger.companion

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.AtomicFile
import com.vibeledger.capture.PendingStore
import com.vibeledger.capture.backendOrigin
import java.io.File
import java.security.KeyStore
import java.security.MessageDigest
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

// Not a data class: accidental toString must never print credentials.
class Binding(val origin: String, val token: String) {
    val namespace: String = MessageDigest.getInstance("SHA-256")
        .digest("$origin\n$token".toByteArray()).joinToString("") { "%02x".format(it) }
}

@Serializable
private data class VaultData(
    val origin: String? = null,
    val token: String? = null,
    val pending: Map<String, String> = emptyMap(),
)

/** AES-GCM key never leaves Android Keystore. All data lives in noBackupFilesDir. */
class CredentialVault(context: Context) {
    private val file = AtomicFile(File(context.noBackupFilesDir, "capture-vault.v1"))
    private val alias = "vibeledger.capture.v1"
    private val json = Json { ignoreUnknownKeys = true }
    private val keyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
    private var data = load()

    private fun key(create: Boolean): SecretKey {
        (keyStore.getKey(alias, null) as? SecretKey)?.let { return it }
        check(create) // Never replace an unreadable vault and silently lose pending keys.
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").apply {
            init(KeyGenParameterSpec.Builder(alias,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256).build())
        }.generateKey()
    }

    private fun load(): VaultData {
        if (!file.baseFile.exists() && !File(file.baseFile.path + ".bak").exists()) return VaultData()
        val raw = file.readFully()
        check(raw.size >= 30 && raw[0].toInt() == 1)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, key(false), GCMParameterSpec(128, raw.copyOfRange(1, 13)))
        return json.decodeFromString(cipher.doFinal(raw.copyOfRange(13, raw.size)).toString(Charsets.UTF_8))
    }

    @Synchronized private fun save(next: VaultData) {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key(true))
        val ciphertext = cipher.doFinal(json.encodeToString(VaultData.serializer(), next).toByteArray())
        val stream = file.startWrite()
        try {
            stream.write(byteArrayOf(1) + cipher.iv + ciphertext)
            file.finishWrite(stream) // AtomicFile syncs to disk before returning.
            data = next
        } catch (failure: Exception) {
            file.failWrite(stream)
            throw failure
        }
    }

    @Synchronized fun binding(): Binding? = data.origin?.let { origin -> data.token?.let { Binding(origin, it) } }
    @Synchronized fun bind(origin: String, token: String): Binding {
        val binding = Binding(backendOrigin(origin).toString(), token.trim())
        save(data.copy(origin = binding.origin, token = binding.token))
        return binding
    }
    @Synchronized fun clearCredentials() { save(data.copy(token = null)) }
    @Synchronized fun otherPendingCount(): Int = data.pending.keys.count { it != binding()?.namespace }

    fun pendingStore(binding: Binding): PendingStore = object : PendingStore {
        override fun read(): String? = synchronized(this@CredentialVault) { data.pending[binding.namespace] }
        override fun write(key: String) = synchronized(this@CredentialVault) {
            check(data.pending[binding.namespace] == null)
            save(data.copy(pending = data.pending + (binding.namespace to key)))
        }
        override fun clear() = synchronized(this@CredentialVault) {
            save(data.copy(pending = data.pending - binding.namespace))
        }
    }
}
