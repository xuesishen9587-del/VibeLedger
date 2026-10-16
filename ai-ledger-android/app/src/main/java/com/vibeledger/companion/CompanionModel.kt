package com.vibeledger.companion

import android.app.Application
import android.net.Uri
import com.vibeledger.capture.CaptureSession
import com.vibeledger.capture.CaptureState
import com.vibeledger.capture.HttpCaptureApi
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

class CompanionApplication : Application() {
    val model by lazy { CompanionModel(this) }
}

data class ScreenState(
    val configured: Boolean = false,
    val origin: String = "",
    val connected: Boolean = false,
    val loading: Boolean = false,
    val imageReady: Boolean = false,
    val imageSequence: Long = 0,
    val capture: CaptureState = CaptureState(),
    val notice: String = "",
    val storageUnavailable: Boolean = false,
    val otherPending: Int = 0,
)

/** Application scope survives Activity recreation; neither intents nor financial bodies are saved. */
class CompanionModel(private val app: Application) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val mutable = MutableStateFlow(ScreenState())
    val state = mutable.asStateFlow()
    private var vault: CredentialVault? = null
    private var session: CaptureSession? = null
    private var observer: Job? = null
    private var image: ByteArray? = null

    init {
        try {
            vault = CredentialVault(app)
            vault?.binding()?.let { activate(it) }
            mutable.update { it.copy(otherPending = vault!!.otherPendingCount()) }
        } catch (_: Exception) {
            mutable.value = ScreenState(storageUnavailable = true,
                notice = "无法读取本机安全存储。为保留未确定请求，已停止上传。请先在网页核对待处理记录。")
        }
    }

    private fun activate(binding: Binding) {
        observer?.cancel()
        val api = HttpCaptureApi(binding.origin, binding.token)
        val active = CaptureSession(api, requireNotNull(vault).pendingStore(binding))
        if (active.state.value.pending) clearImage()
        session = active
        mutable.update { it.copy(configured = true, origin = binding.origin, connected = false,
            capture = active.state.value, otherPending = vault!!.otherPendingCount()) }
        observer = scope.launch {
            active.state.collect { capture -> mutable.update { it.copy(capture = capture,
                connected = if (capture.credentialsInvalid) false else it.connected) } }
        }
        scope.launch {
            if (active.state.value.pending) active.recover()
            try {
                api.checkConnection()
                if (session === active) mutable.update { it.copy(connected = true) }
            } catch (_: Exception) {
                if (session === active) mutable.update { it.copy(notice = "连接尚未验证，请检查网络或设备凭据。") }
            }
        }
    }

    fun bind(origin: String, token: String) {
        if (locked() || state.value.storageUnavailable) return
        val replacingBinding = state.value.configured
        mutable.update { it.copy(loading = true, notice = "") }
        scope.launch {
            try {
                // Verify before replacing the active binding. No token is returned in the UI state.
                val api = HttpCaptureApi(origin, token.trim())
                api.checkConnection()
                val binding = requireNotNull(vault).bind(origin, token)
                if (replacingBinding) clearImage()
                activate(binding)
                mutable.update { it.copy(connected = true, notice = "设备已连接。") }
            } catch (_: Exception) {
                mutable.update { it.copy(notice = "连接失败。请使用 HTTPS 后端地址和有效的独立设备 Token。") }
            } finally { mutable.update { it.copy(loading = false) } }
        }
    }

    fun clearCredentials() {
        if (locked()) return
        try {
            requireNotNull(vault).clearCredentials()
            observer?.cancel()
            session = null
            clearImage()
            mutable.value = ScreenState(otherPending = vault!!.otherPendingCount(),
                notice = "已清除本机凭据。未确定请求仍安全保留，重新输入原地址和原设备 Token 可恢复。")
        } catch (_: Exception) { mutable.update { it.copy(notice = "安全存储写入失败，凭据未清除。") } }
    }

    private fun locked() = state.value.loading || session?.state?.value?.busy == true
    fun select(uri: Uri) {
        if (locked() || state.value.capture.pending || state.value.storageUnavailable) return
        mutable.update { it.copy(loading = true, notice = "") }
        scope.launch {
            try {
                clearImage()
                image = ImagePreparer.jpeg(app.contentResolver, uri)
                mutable.update { it.copy(imageReady = true, imageSequence = it.imageSequence + 1) }
            } catch (_: Exception) { mutable.update { it.copy(notice = "图片无法读取，请重新选择一张截图。") }
            } finally { mutable.update { it.copy(loading = false) } }
        }
    }

    private fun clearImage() { image?.fill(0); image = null; mutable.update { it.copy(imageReady = false) } }
    fun discardImage() { if (!locked()) clearImage() }
    fun upload(note: String) {
        if (locked() || note.length > 2000) return
        val active = session ?: return
        val jpeg = image ?: return
        image = null
        mutable.update { it.copy(imageReady = false, loading = true, notice = "") }
        scope.launch {
            try { active.start(jpeg, note) }
            finally { jpeg.fill(0); mutable.update { it.copy(loading = false) } }
        }
    }
    fun recover() { if (!locked()) scope.launch { session?.recover() } }
    fun confirm() { if (!locked()) scope.launch { session?.confirm() } }
    fun revise(note: String) { if (!locked()) scope.launch { session?.revise(note) } }
    fun cancel() { if (!locked()) scope.launch { session?.cancel() } }
    fun later() { mutable.update { it.copy(notice = "本次请求已保留。下次打开会先查询结果，也可在网页版处理。") } }
}
