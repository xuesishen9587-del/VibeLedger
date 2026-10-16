package com.vibeledger.capture

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith

class BackendOriginTest {
    @Test fun `only HTTPS origins or api roots accepted`() {
        assertEquals("https://example.invalid/", backendOrigin("https://example.invalid/api/v1/").toString())
        for (value in listOf("http://example.invalid", "https://user:pass@example.invalid",
            "https://example.invalid/other", "https://example.invalid?secret=x", "https://example.invalid/#x")) {
            assertFailsWith<IllegalArgumentException> { backendOrigin(value) }
        }
    }
}
