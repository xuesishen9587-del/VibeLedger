package com.vibeledger.companion

import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.ext.junit.runners.AndroidJUnit4
import java.io.File
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith

/** Synthetic data only. Run on a disposable emulator, never on an installed personal app. */
@RunWith(AndroidJUnit4::class)
class CredentialVaultTest {
    @Test fun encryptedVaultSurvivesReopenAndKeepsIdentityNamespaces() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val vault = CredentialVault(context)
        val binding = vault.bind("https://example.invalid", "synthetic-emulator-token-a")
        val pending = vault.pendingStore(binding)
        pending.read()?.let { pending.clear() }
        pending.write("synthetic-pending-key")
        val raw = File(context.noBackupFilesDir, "capture-vault.v1").readBytes().toString(Charsets.UTF_8)
        assertFalse(raw.contains("synthetic-emulator-token-a"))
        assertFalse(raw.contains("synthetic-pending-key"))
        val reopened = CredentialVault(context)
        assertEquals("synthetic-pending-key", reopened.pendingStore(binding).read())
        reopened.clearCredentials()
        assertNull(CredentialVault(context).binding())
        val other = reopened.bind("https://example.invalid", "synthetic-emulator-token-b")
        assertNull(reopened.pendingStore(other).read())
        assertEquals(1, reopened.otherPendingCount())
        val original = reopened.bind("https://example.invalid", "synthetic-emulator-token-a")
        assertEquals("synthetic-pending-key", reopened.pendingStore(original).read())
        reopened.pendingStore(original).clear()
        reopened.clearCredentials()
    }
}
