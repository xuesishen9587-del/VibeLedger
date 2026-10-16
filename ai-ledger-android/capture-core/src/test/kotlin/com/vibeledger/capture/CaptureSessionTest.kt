package com.vibeledger.capture

import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlin.test.AfterTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.SocketPolicy
import okhttp3.tls.HandshakeCertificates
import okhttp3.tls.HeldCertificate

private const val ID = "00000000-0000-4000-8000-000000000001"
private const val TOKEN = "synthetic-device-token-not-a-credential"
private val JPEG = byteArrayOf(0xff.toByte(), 0xd8.toByte(), 0xff.toByte(), 0xd9.toByte())

private class MemoryPending(var key: String? = null) : PendingStore {
    var writes = 0
    override fun read() = key
    override fun write(key: String) { this.key = key; writes++ }
    override fun clear() { key = null }
}

class CaptureSessionTest {
    private val certificate = HeldCertificate.Builder().addSubjectAlternativeName("localhost").build()
    private val serverCertificates = HandshakeCertificates.Builder().heldCertificate(certificate).build()
    private val clientCertificates = HandshakeCertificates.Builder().addTrustedCertificate(certificate.certificate).build()
    private val server = MockWebServer().apply { useHttps(serverCertificates.sslSocketFactory(), false); start() }
    private val transport = OkHttpClient.Builder().sslSocketFactory(clientCertificates.sslSocketFactory(),
        clientCertificates.trustManager).build()
    private fun api() = HttpCaptureApi(server.url("/").toString(), TOKEN, transport)
    private fun body(status: String = "needs_confirmation", version: Int = 3, ready: Boolean = true,
                     action: String = "confirm", mode: String = "one_off") = """
        {"status":"$status","request_id":"$ID","row_version":$version,
        "confirmation_ready":$ready,"next_action":"$action","display_summary":"合成消费摘要",
        "supplement_prompt":"请核对金额","draft":{"payment_mode":"$mode","original_amount":"38.50",
        "original_currency":"CNY","merchant":"测试商户","occurred_on":"2026-10-01"},
        "warnings":[{"code":"MISSING_ACCOUNT","message":"合成账户未识别"}]}
    """.trimIndent()
    private fun enqueue(text: String = body(), code: Int = 200) { server.enqueue(MockResponse().setResponseCode(code).setBody(text)) }
    private fun request() = assertNotNull(server.takeRequest(2, TimeUnit.SECONDS))
    @AfterTest fun shutdown() { server.shutdown() }

    @Test fun `auto save persists key before upload and clears only at terminal`() = runBlocking {
        val store = MemoryPending()
        enqueue(body("committed", ready = false, action = "done"))
        val session = CaptureSession(api(), store)
        session.start(JPEG, "合成备注")
        val upload = request()
        val json = Json.parseToJsonElement(upload.body.readUtf8()).jsonObject
        assertEquals(1, store.writes)
        assertNotNull(json["idempotency_key"])
        assertTrue(json["captured_at"]!!.jsonPrimitive.content.contains(Regex("Z|[+-]\\d{2}:\\d{2}$")))
        assertEquals("合成备注", json["note"]!!.jsonPrimitive.content)
        assertEquals("image/jpeg", json["image"]!!.jsonObject["mime_type"]!!.jsonPrimitive.content)
        assertEquals("/api/v1/expenses", upload.path)
        assertNull(store.key)
        assertEquals("committed", session.state.value.receipt?.status)
        assertFalse(session.state.value.receipt!!.canConfirm)
    }

    @Test fun `pending revise and confirm always submit displayed version`() = runBlocking {
        val store = MemoryPending()
        val session = CaptureSession(api(), store)
        enqueue(body(ready = false, action = "supplement"))
        session.start(JPEG, "")
        request()
        assertNotNull(store.key)
        assertFalse(session.state.value.receipt!!.canConfirm)
        assertEquals("合成账户未识别", session.state.value.receipt!!.warnings.single().message)
        enqueue(body(version = 4))
        session.revise("金额是38.50元")
        val revision = request()
        assertEquals("/api/v1/ingestion-requests/$ID/revise", revision.path)
        val change = Json.parseToJsonElement(revision.body.readUtf8()).jsonObject
        assertEquals("3", change["expected_version"]!!.jsonPrimitive.content)
        assertEquals("金额是38.50元", change["correction_note"]!!.jsonPrimitive.content)
        assertEquals(4L, session.state.value.receipt!!.version)
        assertTrue(session.state.value.receipt!!.canConfirm)
        enqueue(body("committed", ready = false, action = "done"))
        session.confirm()
        val confirm = request()
        assertEquals("/api/v1/ingestion-requests/$ID/confirm", confirm.path)
        assertEquals("4", Json.parseToJsonElement(confirm.body.readUtf8()).jsonObject["expected_version"]!!.jsonPrimitive.content)
        assertNull(store.key)
        assertEquals(1, store.writes)
    }

    @Test fun `lost upload response and process restart recover original key without second upload`() = runBlocking {
        val store = MemoryPending()
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AFTER_REQUEST))
        CaptureSession(api(), store).start(JPEG, "")
        val upload = request()
        val key = assertNotNull(store.key)
        assertEquals(key, Json.parseToJsonElement(upload.body.readUtf8()).jsonObject["idempotency_key"]!!.jsonPrimitive.content)
        val restarted = CaptureSession(api(), store)
        enqueue(body("committed", ready = false, action = "done"))
        // Even an attempted new capture must recover the previous key first.
        restarted.start(JPEG, "不同备注不应上传")
        assertEquals("GET", request().method)
        assertNull(store.key)
        assertEquals(1, store.writes)
        assertEquals(2, server.requestCount)
    }

    @Test fun `409 reloads current draft and requires a new explicit confirmation`() = runBlocking {
        val store = MemoryPending("original-key")
        val session = CaptureSession(api(), store)
        enqueue(body())
        session.recover(); request()
        enqueue("""{"error":{"code":"DRAFT_CHANGED"}}""", 409)
        enqueue(body(version = 9))
        session.confirm()
        assertEquals("POST", request().method)
        assertEquals("GET", request().method)
        assertEquals(9L, session.state.value.receipt!!.version)
        assertEquals("original-key", store.key)
        assertTrue(session.state.value.message.contains("重新核对"))
        assertEquals(3, server.requestCount)
    }

    @Test fun `HTTP timeout preserves key until recovery without new upload`() = runBlocking {
        val store = MemoryPending()
        val shortTimeoutApi = HttpCaptureApi(server.url("/").toString(), TOKEN, transport, callTimeoutMillis = 250)
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.NO_RESPONSE))
        CaptureSession(shortTimeoutApi, store).start(JPEG, "")
        request()
        val key = assertNotNull(store.key)
        enqueue(body("committed", ready = false, action = "done"))
        CaptureSession(api(), store).recover()
        assertEquals("/api/v1/ingestion-requests/by-key/$key", request().path)
        assertNull(store.key)
        assertEquals(1, store.writes)
    }

    @Test fun `404 tombstone closes late upload race and accepts minimal rejected response`() = runBlocking {
        val store = MemoryPending("original-key")
        val session = CaptureSession(api(), store)
        enqueue("""{"error":{"code":"REQUEST_NOT_FOUND"}}""", 404)
        enqueue("""{"status":"rejected","request_id":"$ID","display_summary":"已取消"}""")
        session.recover()
        assertEquals("/api/v1/ingestion-requests/by-key/original-key", request().path)
        val tombstone = request()
        assertEquals("POST", tombstone.method)
        assertEquals("/api/v1/ingestion-requests/by-key/original-key/cancel", tombstone.path)
        assertNull(store.key)
        assertEquals(0, store.writes)
    }

    @Test fun `ambiguous tombstone and unrelated 404 retain key`() = runBlocking {
        val store = MemoryPending("original-key")
        val session = CaptureSession(api(), store)
        enqueue("""{"error":{"code":"REQUEST_NOT_FOUND"}}""", 404)
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AFTER_REQUEST))
        session.recover(); request(); request()
        assertEquals("original-key", store.key)
        enqueue("{}", 404)
        session.recover(); request()
        assertEquals(3, server.requestCount)
        assertNull(session.state.value.receipt)
        assertTrue(session.state.value.pending)
    }

    @Test fun `revoked token retains pending key and disables stale confirmation`() = runBlocking {
        val store = MemoryPending("original-key")
        val session = CaptureSession(api(), store)
        enqueue(body()); session.recover(); request()
        enqueue("""{"error":{"code":"DEVICE_REVOKED"}}""", 401)
        session.confirm(); request()
        assertTrue(session.state.value.credentialsInvalid)
        assertNull(session.state.value.receipt)
        assertEquals("original-key", store.key)
    }

    @Test fun `dependency failure is recovered as durable failed and never reruns AI`() = runBlocking {
        val store = MemoryPending()
        val session = CaptureSession(api(), store)
        enqueue("""{"error":{"code":"CAPTURE_DEPENDENCY_UNAVAILABLE","retryable":true}}""", 503)
        session.start(JPEG, ""); request()
        assertTrue(session.state.value.pending)
        enqueue(body("failed", ready = false, action = "done"))
        session.recover()
        assertEquals("GET", request().method)
        assertEquals("failed", session.state.value.receipt!!.status)
        assertNull(store.key)
        assertEquals(1, store.writes)
    }

    @Test fun `processing and next action done do not clear pending`() = runBlocking {
        val store = MemoryPending("original-key")
        val session = CaptureSession(api(), store)
        enqueue(body("processing", ready = false, action = "done"), 202)
        session.recover(); request()
        assertTrue(session.state.value.pending)
        enqueue(body("rejected", ready = false, action = "done"))
        session.cancel()
        assertEquals("/api/v1/ingestion-requests/by-key/original-key/cancel", request().path)
        assertNull(store.key)
    }

    @Test fun `cancel reviewed draft uses reject with expected version`() = runBlocking {
        val store = MemoryPending("original-key")
        val session = CaptureSession(api(), store)
        enqueue(body()); session.recover(); request()
        enqueue(body("rejected", ready = false, action = "done")); session.cancel()
        val reject = request()
        assertEquals("/api/v1/ingestion-requests/$ID/reject", reject.path)
        assertEquals("3", Json.parseToJsonElement(reject.body.readUtf8()).jsonObject["expected_version"]!!.jsonPrimitive.content)
        assertNull(store.key)
    }

    @Test fun `cancel that races a commit shows saved result without claiming rollback`() = runBlocking {
        val store = MemoryPending("original-key")
        val session = CaptureSession(api(), store)
        enqueue(body("committed", ready = false, action = "done"))
        session.cancel(); request()
        assertEquals("committed", session.state.value.receipt!!.status)
        assertNull(store.key)
    }

    @Test fun `coroutine interruption leaves durable key and no actionable stale snapshot`() = runBlocking {
        val store = MemoryPending()
        val entered = CompletableDeferred<Unit>()
        val fake = object : CaptureApi by api() {
            override suspend fun upload(key: String, jpeg: ByteArray, note: String): Receipt {
                // Assert durability at the boundary before any upload could be sent.
                assertEquals(key, store.read())
                entered.complete(Unit)
                CompletableDeferred<Unit>().await()
                error("unreachable")
            }
        }
        val session = CaptureSession(fake, store)
        val job = async { session.start(JPEG, "") }
        entered.await(); job.cancelAndJoin()
        assertNotNull(store.key)
        assertTrue(session.state.value.pending)
        assertFalse(session.state.value.busy)
        assertNull(session.state.value.receipt)
        assertEquals(0, server.requestCount)
    }

    @Test fun `installments and server gates prevent confirmation or mobile revision`() = runBlocking {
        for ((ready, action, mode) in listOf(Triple(false, "confirm", "one_off"),
            Triple(true, "supplement", "one_off"), Triple(true, "review_in_web", "one_off"),
            Triple(true, "confirm", "installment"))) {
            val session = CaptureSession(api(), MemoryPending("original-key"))
            enqueue(body(ready = ready, action = action, mode = mode)); session.recover(); request()
            assertFalse(session.state.value.receipt!!.canConfirm)
            session.confirm()
            if (mode == "installment" || action == "review_in_web") {
                assertFalse(session.state.value.receipt!!.canRevise)
                session.revise("不得绕过分期规则")
            }
        }
        assertEquals(4, server.requestCount)
    }

    @Test fun `redirect never forwards bearer token to another origin`() = runBlocking {
        val target = MockWebServer()
        target.start()
        try {
            server.enqueue(MockResponse().setResponseCode(307).setHeader("Location", target.url("/leak")))
            val store = MemoryPending("original-key")
            CaptureSession(api(), store).recover()
            assertEquals("Bearer $TOKEN", request().getHeader("Authorization"))
            assertEquals(0, target.requestCount)
            assertEquals("original-key", store.key)
        } finally { target.shutdown() }
    }

    @Test fun `invalid JSON preserves key and removes actionable draft`() = runBlocking {
        val session = CaptureSession(api(), MemoryPending("original-key"))
        enqueue(body()); session.recover(); request()
        enqueue("untrusted invalid response"); session.confirm(); request()
        assertTrue(session.state.value.pending)
        assertNull(session.state.value.receipt)
    }

    @Test fun `failed key persistence prevents any HTTP request`() = runBlocking {
        val store = object : PendingStore {
            override fun read(): String? = null
            override fun write(key: String) { error("synthetic disk failure") }
            override fun clear() = Unit
        }
        CaptureSession(api(), store).start(JPEG, "")
        assertEquals(0, server.requestCount)
    }

    @Test fun `simultaneous taps are dropped before a second key can exist`() = runBlocking {
        val entered = CompletableDeferred<Unit>()
        val release = CompletableDeferred<Unit>()
        var uploads = 0
        val fake = object : CaptureApi {
            override suspend fun upload(key: String, jpeg: ByteArray, note: String): Receipt {
                uploads++; entered.complete(Unit); release.await()
                return Receipt("committed", ID)
            }
            override suspend fun recover(key: String): Receipt = error("Unexpected recovery")
            override suspend fun cancel(key: String): Receipt = error("Unexpected cancel")
            override suspend fun confirm(id: String, version: Long): Receipt = error("Unexpected confirm")
            override suspend fun reject(id: String, version: Long): Receipt = error("Unexpected reject")
            override suspend fun revise(id: String, version: Long, note: String): Receipt = error("Unexpected revise")
        }
        val store = MemoryPending()
        val session = CaptureSession(fake, store)
        val first = async { session.start(JPEG, "") }
        entered.await()
        val duplicate = async { session.start(JPEG, "") }
        duplicate.await(); release.complete(Unit); first.await()
        assertEquals(1, uploads)
        assertEquals(1, store.writes)
        assertNull(store.key)
    }
}
