package com.vibeledger.companion

import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.vibeledger.capture.Receipt
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull

class MainActivity : ComponentActivity() {
    private val model get() = (application as CompanionApplication).model
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
        enableEdgeToEdge()
        if (savedInstanceState == null) receive(intent)
        setContent { CompanionScreen(model) }
    }
    override fun onNewIntent(intent: Intent) { super.onNewIntent(intent); receive(intent) }
    private fun receive(incoming: Intent) {
        if (incoming.action == Intent.ACTION_SEND && incoming.type?.startsWith("image/") == true) {
            val uri = if (Build.VERSION.SDK_INT >= 33) incoming.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
                else @Suppress("DEPRECATION") (incoming.getParcelableExtra(Intent.EXTRA_STREAM) as? Uri)
            uri?.takeIf { it.scheme == "content" }?.let { model.select(it) }
        }
        // Never retain a shared URI in Activity saved state or replay it after rotation.
        setIntent(Intent())
    }
}

private val Teal = Color(0xFF167D72)

@Composable
private fun CompanionScreen(model: CompanionModel) {
    val state by model.state.collectAsStateWithLifecycle()
    var settings by remember { mutableStateOf(false) }
    var note by remember(state.imageSequence) { mutableStateOf("") }
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.GetContent()) { uri ->
        uri?.let { note = ""; model.select(it) }
    }
    val busy = state.loading || state.capture.busy
    MaterialTheme(colorScheme = lightColorScheme(primary = Teal, background = Color(0xFFF5F7F6))) {
        Scaffold { padding ->
            Column(Modifier.fillMaxSize().padding(padding).imePadding().verticalScroll(rememberScrollState())
                .padding(20.dp).widthIn(max = 600.dp), verticalArrangement = Arrangement.spacedBy(18.dp)) {
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Text("轻记", color = Color.White, fontWeight = FontWeight.Bold,
                        modifier = Modifier.background(Teal, RoundedCornerShape(14.dp)).padding(14.dp))
                    Column(Modifier.weight(1f).padding(start = 12.dp)) {
                        Text("VibeLedger", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
                        Text("截图记一笔 · 家庭共享账本", style = MaterialTheme.typography.bodySmall)
                    }
                    if (state.configured) TextButton(onClick = { settings = !settings }, enabled = !busy) {
                        Text(if (settings) "返回" else "设置")
                    }
                }
                if (state.notice.isNotBlank()) Notice(state.notice)
                if (state.capture.message.isNotBlank()) Notice(state.capture.message)
                if (state.storageUnavailable) return@Column
                if (!state.configured || settings) {
                    ConnectionSettings(state, busy, model)
                } else {
                    Text(if (state.connected && !state.capture.credentialsInvalid) "● 已连接家庭账本" else "○ 连接待验证",
                        color = Teal, style = MaterialTheme.typography.bodyMedium)
                    if (busy) Row(horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.CenterVertically) {
                        CircularProgressIndicator(Modifier.size(24.dp), strokeWidth = 2.dp)
                        Text(if (state.loading) "正在准备…" else "正在处理，请稍候…")
                    }
                    state.capture.receipt?.let { ReceiptCard(it) }
                    if (state.capture.pending) {
                        val receipt = state.capture.receipt
                        if (receipt?.webOnly == true) Notice("检测到分期或草稿需要网页审核，请在网页端处理。")
                        if (receipt?.canConfirm == true) Button(onClick = { model.confirm() }, enabled = !busy,
                            modifier = Modifier.fillMaxWidth()) { Text("核对无误，确认保存") }
                        if (receipt?.canRevise == true) Supplement(receipt, busy, model)
                        OutlinedButton(onClick = { model.recover() }, enabled = !busy,
                            modifier = Modifier.fillMaxWidth()) { Text("重新查询本次结果") }
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                            TextButton(onClick = { model.later() }, enabled = !busy) { Text("稍后处理") }
                            TextButton(onClick = { model.cancel() }, enabled = !busy) { Text("取消本次记账") }
                        }
                        Text("待处理期间不会上传新截图。网络中断后请先查询，应用重启也会继续恢复。",
                            style = MaterialTheme.typography.bodySmall)
                    } else {
                        Card(colors = CardDefaults.cardColors(containerColor = Color.White)) {
                            Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
                                Text("把消费截图，交给轻记", style = MaterialTheme.typography.titleMedium,
                                    fontWeight = FontWeight.Bold)
                                Text("从相册选择一张截图，或在系统分享菜单中选择 VibeLedger 轻记。")
                                OutlinedButton(onClick = { picker.launch("image/*") }, enabled = !busy,
                                    modifier = Modifier.fillMaxWidth()) { Text(if (state.imageReady) "重新选择截图" else "选择一张截图") }
                                if (state.imageReady) {
                                    Text("✓ 截图已在内存中准备好", color = Teal)
                                    OutlinedTextField(value = note, onValueChange = { if (it.length <= 2000) note = it },
                                        enabled = !busy, label = { Text("原始消费备注（可选）") },
                                        supportingText = { Text("例如：周末家庭聚餐。备注随本次消费保存。") },
                                        modifier = Modifier.fillMaxWidth())
                                    Button(onClick = { val current = note; note = ""; model.upload(current) },
                                        enabled = !busy, modifier = Modifier.fillMaxWidth()) { Text("开始 AI 识别") }
                                    TextButton(onClick = { model.discardImage(); note = "" }, enabled = !busy) { Text("移除截图") }
                                }
                            }
                        }
                    }
                    Text("截图仅发送至你配置的 VibeLedger 后端。设备与账户归属由家庭账本管理。",
                        style = MaterialTheme.typography.bodySmall, color = Color(0xFF64716F))
                }
                Spacer(Modifier.height(12.dp))
            }
        }
    }
}

@Composable
private fun Notice(text: String) {
    Text(text, modifier = Modifier.fillMaxWidth().background(Color(0xFFE6F1EE), RoundedCornerShape(12.dp))
        .padding(14.dp), style = MaterialTheme.typography.bodyMedium)
}

@Composable
private fun ReceiptCard(receipt: Receipt) {
    Card(colors = CardDefaults.cardColors(containerColor = Color.White)) {
        Column(Modifier.fillMaxWidth().padding(20.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text(when (receipt.status) {
                "committed" -> "已保存到账本"
                "needs_confirmation" -> "请核对识别结果"
                "processing" -> "AI 正在识别"
                "rejected" -> "本次请求已取消"
                "failed" -> "本次识别未完成"
                else -> "请查询处理结果"
            }, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold, color = Teal)
            // The backend formats amount/currency/merchant/category/account/date/installments.
            Text(receipt.summary.ifBlank { "请查询处理结果或前往网页核对。" })
            if ((receipt.draft?.get("payment_mode") as? JsonPrimitive)?.contentOrNull == "installment") {
                val period = (receipt.draft?.get("period_no") as? JsonPrimitive)?.contentOrNull
                if (period != null) Text("当前期次：第 $period 期")
            }
            if (receipt.warnings.isNotEmpty()) {
                HorizontalDivider()
                Text("需要留意", fontWeight = FontWeight.Bold)
                receipt.warnings.forEach { Text("• " + it.message.ifBlank { "请在网页核对这项提示。" }) }
            }
        }
    }
}

@Composable
private fun Supplement(receipt: Receipt, busy: Boolean, model: CompanionModel) {
    var correction by remember(receipt.version) { mutableStateOf("") }
    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        if (receipt.supplementPrompt.isNotBlank()) Text(receipt.supplementPrompt)
        OutlinedTextField(value = correction, onValueChange = { if (it.length <= 2000) correction = it },
            enabled = !busy, label = { Text("用一句话补充或修改") }, modifier = Modifier.fillMaxWidth())
        OutlinedButton(onClick = { val current = correction; correction = ""; model.revise(current) },
            enabled = !busy && correction.isNotBlank(), modifier = Modifier.fillMaxWidth()) { Text("更新识别摘要") }
    }
}

@Composable
private fun ConnectionSettings(state: ScreenState, busy: Boolean, model: CompanionModel) {
    var origin by remember(state.origin) { mutableStateOf(state.origin) }
    var token by remember { mutableStateOf("") }
    Card(colors = CardDefaults.cardColors(containerColor = Color.White)) {
        Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
            Text("连接你的家庭账本", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
            Text("在网页「设置 → 记账设备」中选择 Android App，创建设备后将一次性 Token 填入这里。")
            OutlinedTextField(value = origin, onValueChange = { origin = it }, enabled = !busy,
                label = { Text("HTTPS 后端地址") }, singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri), modifier = Modifier.fillMaxWidth())
            OutlinedTextField(value = token, onValueChange = { token = it }, enabled = !busy,
                label = { Text("独立设备 Token") }, singleLine = true,
                visualTransformation = PasswordVisualTransformation(),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password), modifier = Modifier.fillMaxWidth())
            Button(onClick = { val current = token; token = ""; model.bind(origin, current) },
                enabled = !busy && origin.isNotBlank() && token.isNotBlank(), modifier = Modifier.fillMaxWidth()) {
                Text(if (busy) "正在连接…" else "验证并安全保存")
            }
            if (state.configured) {
                Text(if (state.connected) "● 已连接" else "○ 连接待验证", color = Teal)
                TextButton(onClick = { token = ""; model.clearCredentials() }, enabled = !busy) { Text("清除本机凭据") }
            }
            if (state.otherPending > 0) Notice("另有 ${state.otherPending} 个原设备身份的未确定请求保留在本机。输入原地址及原 Token 可恢复；请先在网页核对，避免重复记账。")
            Text("凭据由 Android Keystore 保护，不参与系统备份。清除凭据不会撤销网页上的设备，也不会取消待处理请求。",
                style = MaterialTheme.typography.bodySmall)
        }
    }
}
