package com.vibeledger.capture

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull

@Serializable
data class Receipt(
    val status: String,
    @SerialName("request_id") val requestId: String,
    @SerialName("display_summary") val summary: String = "",
    @SerialName("row_version") val version: Long? = null,
    @SerialName("confirmation_ready") val confirmationReady: Boolean = false,
    @SerialName("next_action") val nextAction: String = "recover",
    @SerialName("supplement_prompt") val supplementPrompt: String = "",
    val draft: JsonObject? = null,
    val warnings: List<CaptureWarning> = emptyList(),
) {
    val terminal get() = status in setOf("committed", "rejected", "failed")
    val webOnly get() = nextAction == "review_in_web" ||
        (draft?.get("payment_mode") as? JsonPrimitive)?.contentOrNull == "installment"
    val canConfirm get() = status == "needs_confirmation" && confirmationReady &&
        nextAction == "confirm" && !webOnly && version != null && version >= 0 && summary.isNotBlank()
    val canRevise get() = status == "needs_confirmation" && !webOnly &&
        nextAction in setOf("confirm", "supplement") && version != null
}

@Serializable
data class CaptureWarning(val code: String = "", val message: String = "")

/** Durable storage contains only a key, never screenshots, notes or receipt bodies. */
interface PendingStore {
    fun read(): String?
    /** Must synchronously commit before returning, or throw without uploading. */
    fun write(key: String)
    fun clear()
}

data class CaptureState(
    val busy: Boolean = false,
    val pending: Boolean = false,
    val receipt: Receipt? = null,
    val message: String = "",
    val credentialsInvalid: Boolean = false,
)
