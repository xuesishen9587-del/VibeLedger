package com.vibeledger.capture

import java.io.IOException
import java.time.OffsetDateTime
import java.util.Base64
import java.util.UUID
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.put
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

interface CaptureApi {
    suspend fun upload(key: String, jpeg: ByteArray, note: String): Receipt
    suspend fun recover(key: String): Receipt
    suspend fun cancel(key: String): Receipt
    suspend fun revise(id: String, version: Long, note: String): Receipt
    suspend fun confirm(id: String, version: Long): Receipt
    suspend fun reject(id: String, version: Long): Receipt
}

class ApiFailure(val status: Int, val code: String) : IOException("HTTP $status ($code)")

/** Accept an HTTPS origin (optionally suffixed /api/v1); never accept URL credentials. */
fun backendOrigin(value: String): String {
    val url = value.trim().toHttpUrl()
    require(url.isHttps && url.username.isEmpty() && url.password.isEmpty() &&
        url.query == null && url.fragment == null &&
        url.encodedPath.trimEnd('/') in setOf("", "/api/v1"))
    return url.newBuilder().encodedPath("/").build().toString()
}

class HttpCaptureApi(
    origin: String,
    private val token: String,
    transport: OkHttpClient = OkHttpClient(),
    callTimeoutMillis: Long = 80_000,
) : CaptureApi {
    private val base = backendOrigin(origin).toHttpUrl()
    // Re-apply security controls even for injected transports. No automatic POST replay.
    private val client = transport.newBuilder().followRedirects(false).followSslRedirects(false)
        .retryOnConnectionFailure(false).cache(null)
        .connectTimeout(15, TimeUnit.SECONDS).readTimeout(65, TimeUnit.SECONDS)
        .callTimeout(callTimeoutMillis.also { require(it in 1..80_000) }, TimeUnit.MILLISECONDS).build()
    private val json = Json { ignoreUnknownKeys = true }

    init { require(token.isNotBlank() && token.none { it.isWhitespace() || it.isISOControl() }) }

    private fun url(vararg segments: String): HttpUrl = base.newBuilder()
        .addPathSegments("api/v1").apply { segments.forEach { addPathSegment(it) } }.build()

    private suspend fun request(method: String, segments: Array<out String>, body: String? = null): String =
        withContext(Dispatchers.IO) {
            val request = Request.Builder().url(url(*segments))
                .header("Authorization", "Bearer $token").header("Accept", "application/json")
                .method(method, if (method == "GET") null else
                    (body ?: "").toRequestBody("application/json; charset=utf-8".toMediaType())).build()
            client.newCall(request).execute().use { response ->
                // Bound untrusted responses; never expose error bodies, URLs or exception text to UI/logs.
                val source = response.body?.source() ?: throw IOException("Empty response")
                if (source.request(1_048_577)) throw IOException("Response too large")
                val text = source.readUtf8()
                if (!response.isSuccessful) {
                    val code = runCatching {
                        json.parseToJsonElement(text).jsonObject["error"]?.jsonObject
                            ?.get("code")?.jsonPrimitive?.contentOrNull
                    }.getOrNull()?.takeIf { it.matches(Regex("[A-Z0-9_]{1,80}")) } ?: "REQUEST_FAILED"
                    throw ApiFailure(response.code, code)
                }
                text
            }
        }

    private suspend fun receipt(method: String, vararg path: String, body: String? = null): Receipt {
        val result = json.decodeFromString<Receipt>(request(method, path, body))
        require(result.status in setOf("processing", "needs_confirmation", "committed", "rejected", "failed"))
        UUID.fromString(result.requestId)
        return result
    }

    /** Read-only authentication check; returns no financial data and stores no response. */
    suspend fun checkConnection() { request("GET", arrayOf("devices")) }

    override suspend fun upload(key: String, jpeg: ByteArray, note: String): Receipt {
        require(jpeg.isNotEmpty() && jpeg.size <= 10 * 1024 * 1024 && note.length <= 2000)
        val body = buildJsonObject {
            put("idempotency_key", key)
            put("captured_at", OffsetDateTime.now().toString())
            put("client_version", "android/0.1.0")
            put("note", note)
            put("image", buildJsonObject {
                put("mime_type", "image/jpeg")
                put("base64", Base64.getEncoder().encodeToString(jpeg))
            })
        }
        return receipt("POST", "expenses", body = body.toString())
    }
    override suspend fun recover(key: String) = receipt("GET", "ingestion-requests", "by-key", key)
    override suspend fun cancel(key: String) = receipt("POST", "ingestion-requests", "by-key", key, "cancel")
    private suspend fun action(id: String, action: String, version: Long, note: String? = null): Receipt {
        UUID.fromString(id)
        val body = buildJsonObject {
            put("expected_version", version)
            if (note != null) put("correction_note", note)
        }
        return receipt("POST", "ingestion-requests", id, action, body = body.toString())
    }
    override suspend fun revise(id: String, version: Long, note: String) = action(id, "revise", version, note)
    override suspend fun confirm(id: String, version: Long) = action(id, "confirm", version)
    override suspend fun reject(id: String, version: Long) = action(id, "reject", version)
}
