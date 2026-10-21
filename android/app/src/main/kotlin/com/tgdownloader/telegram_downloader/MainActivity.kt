package com.tgdownloader.telegram_downloader

import android.content.Context
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.lifecycle.viewmodel.compose.viewModel
import com.chaquo.python.android.PyApplication
import com.tgdownloader.telegram_downloader.ui.AddScreen
import com.tgdownloader.telegram_downloader.ui.AuthScreen
import com.tgdownloader.telegram_downloader.ui.Destination
import com.tgdownloader.telegram_downloader.ui.OngoingScreen
import com.tgdownloader.telegram_downloader.ui.SettingsScreen
import com.tgdownloader.telegram_downloader.ui.TgBottomBar
import com.tgdownloader.telegram_downloader.ui.TgTheme
import com.tgdownloader.telegram_downloader.ui.TgToast
import com.tgdownloader.telegram_downloader.ui.TgTopBar
import kotlinx.coroutines.delay
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.IOException

/**
 * The application.
 *
 * `PyApplication` starts the Python interpreter before anything else runs,
 * and this subclass then gives the engine its directories -- the config dir
 * and the download folder -- before a single line of `config.py` runs.
 */
class TgApplication : PyApplication() {
    override fun onCreate() {
        super.onCreate()
        try {
            Bridge.ensureInitialized(this)
        } catch (e: Exception) {
            Log.e(TAG, "the engine could not start", e)
        }
    }

    companion object {
        const val TAG = "TgBridge"
    }
}

/** The Compose host. */
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            TgApp()
        }
    }
}

/**
 * The shell: the top bar, the page, the bottom bar, and the toast.
 *
 * The page starts immediately below the chip strip: the tab strip IS the
 * page title, so there is no per-page heading.
 */
@Composable
fun TgApp() {
    val vm: TgViewModel = viewModel()
    val context = LocalContext.current

    // The .txt batch file. The picker returns a content:// URI, not a
    // path, so the BYTES it returns are what gets read -- the same
    // approach the desktop app takes.
    val txtPicker = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocument(),
    ) { uri ->
        if (uri != null) readBatchFile(context, uri, vm)
    }

    // The download folder.
    val folderPicker = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocumentTree(),
    ) { uri ->
        if (uri != null) useFolder(context, uri, vm)
    }

    // The toast fades itself out.
    val notice = vm.notice
    LaunchedEffect(notice?.id) {
        if (notice != null) {
            delay(4500)
            vm.clearNotice()
        }
    }

    TgTheme(dark = vm.dark) {
        Box(
            modifier = Modifier
                .fillMaxSize()
                .background(tgColors().page),
        ) {
            Column(
                modifier = Modifier
                    .fillMaxSize()
                    // safeDrawing covers the status bar, the navigation bar
                    // AND the keyboard: the app draws edge to edge, so
                    // without this the keyboard would cover the link field.
                    .windowInsetsPadding(WindowInsets.safeDrawing),
            ) {
                TgTopBar(
                    selected = vm.destination,
                    onSelect = vm::goTo,
                    dark = vm.dark,
                    onToggleTheme = vm::toggleTheme,
                )
                Box(modifier = Modifier.weight(1f)) {
                    when (vm.destination) {
                        Destination.Add -> AddScreen(
                            vm = vm,
                            onLoadTxt = {
                                txtPicker.launch(
                                    arrayOf("text/plain", "text/*", "application/octet-stream"),
                                )
                            },
                        )
                        Destination.Ongoing -> OngoingScreen(vm)
                        Destination.Auth -> AuthScreen(vm)
                        Destination.Settings -> SettingsScreen(vm)
                    }
                }
                TgBottomBar(
                    folder = vm.settings.downloadDir,
                    onChange = {
                        runCatching { folderPicker.launch(null) }
                            .onFailure { vm.reportFolderError("No folder picker on this device.") }
                    },
                )
            }
            notice?.let {
                Box(
                    modifier = Modifier
                        .align(Alignment.BottomCenter)
                        .navigationBarsPadding(),
                ) {
                    TgToast(text = it.text, isError = it.isError)
                }
            }
        }
    }
}

/**
 * Read a .txt batch file.
 *
 * The chooser hands back a content:// URI, so the bytes are read through
 * the content resolver. The size cap is still checked while reading -- a
 * 4 GB .txt is not a batch file, and reading it first and checking
 * afterwards would be a joke.
 */
private fun readBatchFile(context: Context, uri: Uri, vm: TgViewModel) {
    val max = 256 * 1024
    val text = try {
        context.contentResolver.openInputStream(uri)?.use { input ->
            val buffer = ByteArray(8 * 1024)
            val out = ByteArrayOutputStream()
            var total = 0
            while (true) {
                val read = input.read(buffer)
                if (read <= 0) break
                total += read
                if (total > max) {
                    throw IOException("Batch file too large (max 256 KB). Split it or trim it.")
                }
                out.write(buffer, 0, read)
            }
            String(out.toByteArray(), Charsets.UTF_8)
        } ?: ""
    } catch (e: Exception) {
        vm.reportFileError(e.message ?: "Cannot read the file.")
        return
    }
    vm.setLinksFromFile(text)
}

/**
 * The user picked a folder.
 *
 * The engine writes through the FILESYSTEM, so the picked tree has to be
 * one the app can really write to. The tree URI is resolved to a path, the
 * path is probed, and the folder is only accepted if the probe succeeds --
 * otherwise the app says why and keeps the folder it had.
 */
private fun useFolder(context: Context, uri: Uri, vm: TgViewModel) {
    val path = treePath(context, uri)
    if (path == null) {
        vm.reportFolderError(
            "That folder is not on this device's storage, so the engine " +
                "cannot write to it.",
        )
        return
    }
    if (!canWrite(path)) {
        vm.reportFolderError(
            "Android only lets this app write to its own folder, so " +
                "\"$path\" was not accepted. The download folder is unchanged.",
        )
        return
    }
    vm.changeDownloadDir(path)
}

/** The filesystem path of a picked tree, or null when there is not one. */
private fun treePath(context: Context, uri: Uri): String? {
    val documentId = try {
        android.provider.DocumentsContract.getTreeDocumentId(uri)
    } catch (e: Exception) {
        null
    } ?: return null
    val parts = documentId.split(":", limit = 2)
    if (parts.size != 2) return null
    val volume = parts[0]
    val relative = parts[1].trim('/')
    val root = when {
        volume.equals("primary", ignoreCase = true) ->
            Environment.getExternalStorageDirectory()?.absolutePath
        else -> externalVolumePath(context, volume)
    } ?: return null
    val path = if (relative.isEmpty()) root else "$root/$relative"
    return File(path).absolutePath
}

/** The mount point of a removable volume, when it has one. */
private fun externalVolumePath(context: Context, volumeId: String): String? {
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
        val manager = context.getSystemService(Context.STORAGE_SERVICE)
            as? android.os.storage.StorageManager
        manager?.storageVolumes?.forEach { volume ->
            if (volume.uuid.toString().equals(volumeId, ignoreCase = true)) {
                return volume.directory?.absolutePath
            }
        }
    }
    // The usual mount point for a removable volume.
    val candidate = "/storage/$volumeId"
    return if (File(candidate).isDirectory) candidate else null
}

/** Can the app really write here? The answer decides whether it is used. */
private fun canWrite(path: String): Boolean = try {
    val dir = File(path)
    if (!dir.isDirectory && !dir.mkdirs()) return false
    val probe = File(dir, ".telegram-downloader-write-probe")
    probe.writeText("ok")
    probe.delete()
    true
} catch (e: Exception) {
    false
}
