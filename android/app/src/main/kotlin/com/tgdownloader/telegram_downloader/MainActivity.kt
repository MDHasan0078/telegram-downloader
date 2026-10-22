package com.tgdownloader.telegram_downloader

import android.content.Context
import android.content.ContextWrapper
import android.content.Intent
import android.graphics.drawable.ColorDrawable
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.provider.Settings
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
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
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.platform.LocalContext
import androidx.core.content.FileProvider
import androidx.lifecycle.viewmodel.compose.viewModel
import com.chaquo.python.android.PyApplication
import com.tgdownloader.telegram_downloader.TgPalette
import com.tgdownloader.telegram_downloader.TgTheme
import com.tgdownloader.telegram_downloader.ui.AddScreen
import com.tgdownloader.telegram_downloader.ui.AuthScreen
import com.tgdownloader.telegram_downloader.ui.Destination
import com.tgdownloader.telegram_downloader.ui.OngoingScreen
import com.tgdownloader.telegram_downloader.ui.SettingsScreen
import com.tgdownloader.telegram_downloader.ui.TgBottomBar
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
        // The system bars follow the APP's theme, not the system's: the
        // app has its own light/dark switch and the system does not know
        // about it. The dark theme is the one the app ships in, so that is
        // what the first frame gets.
        applySystemBars(dark = true)
        setContent {
            TgApp()
        }
    }
}

/**
 * The system bars, in the app's theme.
 *
 * The window draws edge to edge, so the bars are transparent and the
 * app's own page colour is behind them. Without this the navigation bar
 * is a light slab under a dark app, with dark icons on it, and it cannot
 * follow the app's own theme switch at all.
 */
private fun ComponentActivity.applySystemBars(dark: Boolean) {
    val page = if (dark) TgPalette.Dark.page else TgPalette.Light.page
    val scrim = page.toArgb()
    val style = if (dark) {
        SystemBarStyle.dark(scrim)
    } else {
        SystemBarStyle.light(scrim, scrim)
    }
    enableEdgeToEdge(
        statusBarStyle = style,
        navigationBarStyle = style,
    )
    // The area behind the bars is the WINDOW's background, not the app's:
    // the app insets its own content, so the window is what shows under
    // the system bars. It has to be the page colour, or the bars sit on
    // a slab of whatever the platform theme was.
    window.setBackgroundDrawable(ColorDrawable(scrim))
}

/** The activity behind this context, whatever wraps it. */
private tailrec fun Context.findActivity(): ComponentActivity? = when (this) {
    is ComponentActivity -> this
    is ContextWrapper -> baseContext.findActivity()
    else -> null
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

    // The system bars are the app's own: when the theme is flipped, they
    // are re-painted with it. The platform cannot do this on its own --
    // it only knows the system's night mode, and this app has a switch of
    // its own.
    val activity = context.findActivity()
    LaunchedEffect(vm.dark) {
        activity?.applySystemBars(vm.dark)
    }

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
                        Destination.Settings -> SettingsScreen(
                            vm = vm,
                            onInstallUpdate = {
                                installUpdate(context, vm.updateDownload.path, vm)
                            },
                        )
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

/**
 * Hand the downloaded update package to the system installer.
 *
 * The package is in the app's own storage, which the installer cannot
 * read, so it is shared through the FileProvider: the installer gets a
 * content:// URI with a one-shot read grant and nothing else in the app
 * is exposed.
 *
 * Android 8 and later need the app to be allowed to install packages
 * before the installer opens. That is a per-app setting, so the app
 * sends the user to it and says what to do next, rather than firing an
 * intent that fails.
 */
private fun installUpdate(context: Context, path: String, vm: TgViewModel) {
    val file = File(path)
    if (path.isEmpty() || !file.exists()) {
        vm.reportUpdateError("The downloaded package is not there any more.")
        return
    }
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O &&
        !context.packageManager.canRequestPackageInstalls()
    ) {
        runCatching {
            context.startActivity(
                Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES).apply {
                    data = Uri.parse("package:${context.packageName}")
                }
            )
        }
        vm.reportUpdateError(
            "Allow Telegram Downloader to install updates, then tap Install again."
        )
        return
    }
    runCatching {
        val uri = FileProvider.getUriForFile(
            context, "${context.packageName}.files", file
        )
        context.startActivity(
            Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(uri, "application/vnd.android.package-archive")
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
        )
    }.onFailure {
        vm.reportUpdateError(it.message ?: "Could not start the installer.")
    }
}
