package com.vibeledger.capture

import java.util.UUID
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.sync.Mutex

/** One application-scoped session. Contended taps are dropped, never queued as new uploads. */
class CaptureSession(private val api: CaptureApi, private val store: PendingStore) {
    private val mutex = Mutex()
    private val mutable = MutableStateFlow(CaptureState(pending = store.read() != null))
    val state = mutable.asStateFlow()

    private fun accept(receipt: Receipt) {
        // Only server terminal statuses permit clearing. next_action=done is insufficient.
        if (receipt.terminal) store.clear()
        mutable.value = CaptureState(busy = true, pending = store.read() != null, receipt = receipt)
    }

    private suspend fun recoverKey(key: String) {
        val result = try { api.recover(key) } catch (failure: ApiFailure) {
            if (failure.status != 404 || failure.code != "REQUEST_NOT_FOUND") throw failure
            // Close the late-upload race before allowing any new key.
            api.cancel(key)
        }
        accept(result)
    }

    private suspend fun operation(block: suspend () -> Unit) {
        if (!mutex.tryLock()) return
        mutable.value = mutable.value.copy(busy = true, message = "")
        try {
            block()
        } catch (failure: CancellationException) {
            mutable.value = CaptureState(busy = true, pending = store.read() != null,
                message = "操作已中断，请重新查询本次结果。")
            throw failure
        } catch (failure: Exception) {
            // Discard the actionable snapshot after every ambiguous response.
            mutable.value = CaptureState(busy = true, pending = store.read() != null,
                credentialsInvalid = failure is ApiFailure && failure.status in setOf(401, 403),
                message = when {
                    store.read() == null -> "未能安全保存本次请求，未上传截图。请检查设备存储并重新选择。"
                    failure is ApiFailure && failure.status in setOf(401, 403) ->
                        "设备凭据已失效或无权访问。请在设置中检查，待处理请求仍保留。"
                    failure is ApiFailure && failure.status == 409 ->
                        "草稿或请求状态已变化，请核对重新读取的摘要后再操作。"
                    else -> "结果尚未确定，已保留本次请求。请重新查询或稍后处理。"
                })
            if (failure is ApiFailure && failure.status == 409) {
                val key = store.read()
                if (key != null) {
                    try {
                        recoverKey(key)
                        mutable.value = mutable.value.copy(message = "草稿已更新，请重新核对摘要后操作。")
                    } catch (cancelled: CancellationException) {
                        throw cancelled
                    } catch (recoveryFailure: Exception) {
                        // Keep key and invalidated snapshot; never retry the mutating action.
                        if (recoveryFailure is ApiFailure && recoveryFailure.status in setOf(401, 403))
                            mutable.value = mutable.value.copy(credentialsInvalid = true,
                                message = "设备凭据已失效，待处理请求仍保留。")
                    }
                }
            }
        } finally {
            mutable.value = mutable.value.copy(busy = false)
            mutex.unlock()
        }
    }

    suspend fun start(jpeg: ByteArray, note: String) = operation {
        val previous = store.read()
        if (previous != null) { recoverKey(previous); return@operation }
        require(jpeg.isNotEmpty() && jpeg.size <= 10 * 1024 * 1024 && note.length <= 2000)
        val key = UUID.randomUUID().toString()
        store.write(key) // A failed durable write must stop the upload.
        accept(api.upload(key, jpeg, note))
    }

    suspend fun recover() = operation { store.read()?.let { recoverKey(it) } }

    suspend fun confirm() = operation {
        val current = mutable.value.receipt
        if (store.read() != null && current?.canConfirm == true)
            accept(api.confirm(current.requestId, requireNotNull(current.version)))
    }

    suspend fun revise(note: String) = operation {
        val current = mutable.value.receipt
        if (store.read() != null && current?.canRevise == true && note.isNotBlank() && note.length <= 2000)
            accept(api.revise(current.requestId, requireNotNull(current.version), note))
    }

    suspend fun cancel() = operation {
        val key = store.read() ?: return@operation
        val current = mutable.value.receipt
        if (current?.status == "needs_confirmation" && current.version != null)
            accept(api.reject(current.requestId, current.version))
        else accept(api.cancel(key))
    }
}
