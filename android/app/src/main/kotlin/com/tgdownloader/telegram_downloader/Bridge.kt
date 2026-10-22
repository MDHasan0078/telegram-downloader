package com.tgdownloader.telegram_downloader

import android.content.Context
import android.os.Environment
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/** A Python-side failure, with the message Python gave. */
class BridgeException(message: String, cause: Throwable? = null) : Exception(message, cause)

/** The persisted settings, as the app shows them. */
data class AppSettings(
    val apiId: String = "",
    val apiHash: String = "",
    val hasApi: Boolean = false,
    val configured: Boolean = false,
    val downloadDir: String = "",
    val outputMode: String = "1",
    val theme: String = "dark",
    val userId: String = "",
)

/** One row of the download queue. */
data class QueueItem(
    val id: String = "",
    val url: String = "",
    val title: String = "",
    val ext: String = "",
    val mode: String = "1",
    val size: Long = 0,
    val status: String = "queued",
    val uiStatus: String = "waiting",
    val progress: Float = 0f,
    val currentBytes: Long = 0,
    val totalBytes: Long = 0,
    val error: String = "",
    val dest: String = "",
    val detail: String = "",
    val idleLabel: String = "",
)

/** One dependency, with the truth about this platform. */
data class DependencyRow(
    val name: String = "",
    val ok: Boolean = false,
    val version: String = "",
    val hint: String = "",
    val install: String = "",
    val required: Boolean = false,
    /**
     * False for a dependency that does not apply to this platform at
     * all: the flet row belongs to the desktop app -- the .deb ships
     * flet for `tg-dl gui --flet` and for the web build -- and this app
     * is Kotlin/Compose and never imports it. There is nothing the user
     * can do about it here, so the screen does not show it.
     */
    val applicable: Boolean = true,
)

/** What this build can actually do. */
data class Capabilities(
    val platform: String = "android",
    val ffmpeg: Boolean = false,
    val modes: List<String> = listOf("1"),
    val version: String = "",
    val telethon: String = "",
)

/** The result of checking the credentials against Telegram. */
data class VerifyResult(
    val ok: Boolean = false,
    val message: String = "",
)

/** The result of queueing links. */
data class AddResult(
    val ok: Boolean = false,
    val added: Int = 0,
    val requested: Int = 0,
    val error: String = "",
)

/** The result of asking the update server. */
data class UpdateResult(
    val ok: Boolean = false,
    val version: String = "",
    val url: String = "",
    val current: String = "",
    val newer: Boolean = false,
    val error: String = "",
)

/**
 * The state of the update download, as the Python side sees it.
 *
 * `status` is "idle" | "downloading" | "done" | "error"; `progress` is
 * 0..1 and is 0 while the total size is still unknown, which is when
 * the bar has to be indeterminate rather than pinned at zero.
 */
data class UpdateDownloadState(
    val status: String = "idle",
    val progress: Float = 0f,
    val done: Long = 0L,
    val total: Long = 0L,
    val detail: String = "",
    val error: String = "",
    val path: String = "",
    val version: String = "",
    val name: String = "",
) {
    val downloading: Boolean get() = status == "downloading"
    val doneOk: Boolean get() = status == "done"
    val failed: Boolean get() = status == "error"
    val known: Boolean get() = total > 0L
}

/**
 * Kotlin -> Python, over Chaquopy.
 *
 * There is no socket and no HTTP: the Python engine runs IN-PROCESS with
 * the app, and this class calls it directly.
 *
 * THE THREADING RULE: every call here is a `suspend` function that hops to
 * `Dispatchers.IO` before it touches Python. The engine is asyncio (telethon
 * requires it) and the Android UI thread must never block: a download that
 * takes minutes must not freeze the screen. The Python side mirrors
 * `gtk/engine.py` -- one event loop in a background thread, and a call that
 * needs the loop hops to that thread and waits there.
 *
 * The structured values Python returns (dicts and lists) are serialised to
 * JSON on the Python side and parsed here: `PyObject` cannot be told apart
 * from a String without guessing at its type, and JSON is exact.
 */
object Bridge {

    private val python: Python get() = Python.getInstance()
    private val module: PyObject by lazy {
        python.getModule("telegram_downloader.bridge")
            ?: throw BridgeException("telegram_downloader.bridge is missing from the APK")
    }
    private val jsonModule: PyObject by lazy {
        python.getModule("json")
            ?: throw BridgeException("the json module is missing from the APK")
    }

    @Volatile
    private var initialized = false
    private val initLock = Any()

    // -- lifecycle --------------------------------------------------------

    /**
     * Give the engine its directories, once.
     *
     * The config directory is the app's own (`<files>/xdg`, owner-only),
     * because `config.py` refuses to start if the directory is a symlink, is
     * owned by somebody else, or is group/world writable. The download
     * folder is the app's own folder on the shared external volume, which
     * is a real path the engine can write to without a runtime permission.
     */
    fun ensureInitialized(context: Context) {
        if (initialized) return
        synchronized(initLock) {
            if (initialized) return
            val app = context.applicationContext
            val external = app.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS)
            val downloads = File(external ?: File(app.filesDir, "Downloads"), "Telegram")
            val status = call("init", app.filesDir.absolutePath, downloads.absolutePath,
                app.cacheDir.absolutePath)
            initialized = true
            // The marker the CI smoke test looks for: it is printed only
            // after Chaquopy is up AND the engine accepted its config
            // directory, which is the Android-specific failure this log
            // exists to catch.
            android.util.Log.i("TgBridge", "engine ready: ${status.asJson()}")
        }
    }

    // -- the plumbing -----------------------------------------------------

    @Suppress("UNCHECKED_CAST")
    private fun call(name: String, vararg args: Any?): PyObject = try {
        module.callAttr(name, *args as Array<Any>)
    } catch (e: Exception) {
        throw BridgeException(e.message ?: "the engine failed", e)
    }

    /** Run a blocking Python call off the UI thread. */
    private suspend fun <T> io(block: () -> T): T = withContext(Dispatchers.IO) { block() }

    private fun PyObject.asJson(): String = jsonModule.callAttr("dumps", this).toString()

    private fun PyObject.asJsonObject(): JSONObject = JSONObject(asJson())

    private fun PyObject.asJsonArray(): JSONArray = JSONArray(asJson())

    /**
     * Map a JSON ARRAY of objects into Kotlin objects.
     *
     * The receiver is a JSONArray, not a JSONObject, and that is not
     * cosmetic: JSONArray has getJSONObject(index) -- the int overload --
     * and JSONObject does not. With a JSONObject receiver this did not
     * compile ("actual type is 'Int', but 'String!' was expected"), and
     * the two call sites that use it are reading arrays.
     *
     * The name is distinct from the stdlib's `map` on purpose: JSONArray is
     * Iterable, so a member extension called `map` would quietly shadow the
     * stdlib one.
     */
    private fun <T> JSONArray.mapObjects(transform: (JSONObject) -> T): List<T> {
        val out = ArrayList<T>()
        for (i in 0 until length()) {
            out.add(transform(getJSONObject(i)))
        }
        return out
    }

    // -- settings ---------------------------------------------------------

    suspend fun settings(): AppSettings = io {
        call("get_settings").asJsonObject().let { o ->
            AppSettings(
                apiId = o.optString("api_id"),
                apiHash = o.optString("api_hash"),
                hasApi = o.optBoolean("has_api"),
                configured = o.optBoolean("configured"),
                downloadDir = o.optString("download_dir"),
                outputMode = o.optString("output_mode", "1"),
                theme = o.optString("theme", "dark"),
                userId = o.optString("user_id"),
            )
        }
    }

    suspend fun setApiCredentials(apiId: String, apiHash: String): Boolean = io {
        call("set_api_credentials", apiId, apiHash).toString() == "True"
    }

    suspend fun setOutputMode(mode: String): Boolean = io {
        call("set_output_mode", mode).toString() == "True"
    }

    suspend fun setTheme(theme: String): Boolean = io {
        call("set_theme", theme).toString() == "True"
    }

    suspend fun downloadDir(): String = io {
        call("get_download_dir").asJsonObject().optString("download_dir")
    }

    /** Validate a folder and store it. Returns the error, or "" on success. */
    suspend fun setDownloadDir(path: String): String = io {
        val o = call("set_download_dir", path).asJsonObject()
        if (o.optBoolean("ok")) "" else o.optString("error", "Cannot use this folder.")
    }

    // -- the queue ---------------------------------------------------------

    suspend fun queueItems(): List<QueueItem> = io {
        call("get_queue_items").asJsonArray().mapObjects { o ->
            QueueItem(
                id = o.optString("id"),
                url = o.optString("url"),
                title = o.optString("title"),
                ext = o.optString("ext"),
                mode = o.optString("mode", "1"),
                size = o.optLong("size"),
                status = o.optString("status"),
                uiStatus = o.optString("ui_status", "waiting"),
                progress = o.optDouble("progress", 0.0).toFloat(),
                currentBytes = o.optLong("current_bytes"),
                totalBytes = o.optLong("total_bytes"),
                error = o.optString("error"),
                dest = o.optString("dest"),
                detail = o.optString("detail"),
                idleLabel = o.optString("idle_label"),
            )
        }
    }

    /** Split pasted text into clean Telegram URLs, with the engine's rules. */
    suspend fun parseLinks(raw: String): List<String> = io {
        val arr = call("parse_links", raw).asJsonArray()
        (0 until arr.length()).map { arr.getString(it) }
    }

    /** Queue links and start the run. */
    suspend fun addLinks(urls: List<String>): AddResult = io {
        call("add_links", urls).asJsonObject().let { o ->
            AddResult(
                ok = o.optBoolean("ok"),
                added = o.optInt("added"),
                requested = o.optInt("requested"),
                error = o.optString("error"),
            )
        }
    }

    suspend fun retryItem(id: String): Boolean = io {
        call("retry_item", id).toString() == "True"
    }

    suspend fun removeItem(id: String): Boolean = io {
        call("remove_item", id).toString() == "True"
    }

    suspend fun pauseItem(id: String): Boolean = io {
        call("pause_item", id).toString() == "True"
    }

    suspend fun clearFinished(): Boolean = io {
        call("clear_finished").toString() == "True"
    }

    suspend fun clearQueue(): Boolean = io {
        call("clear_queue").toString() == "True"
    }

    suspend fun startQueue(): Boolean = io {
        call("start_queue").toString() == "True"
    }

    suspend fun cancelBatch(): Boolean = io {
        call("cancel_batch").toString() == "True"
    }

    // -- the account -------------------------------------------------------

    suspend fun capabilities(): Capabilities = io {
        call("get_capabilities").asJsonObject().let { o ->
            val modes = o.optJSONArray("modes")
            Capabilities(
                platform = o.optString("platform", "android"),
                ffmpeg = o.optBoolean("ffmpeg"),
                modes = (0 until (modes?.length() ?: 0)).map { modes!!.getString(it) },
                version = o.optString("version"),
                telethon = o.optString("telethon"),
            )
        }
    }

    suspend fun verifyCredentials(): VerifyResult = io {
        val o = call("verify_credentials").asJsonObject()
        val ok = o.optBoolean("ok")
        VerifyResult(
            ok = ok,
            message = if (ok) {
                o.optString("message", "Credentials accepted.")
            } else {
                o.optString("error", "Verification failed.")
            },
        )
    }

    /** Start the login. Returns the state: "code", "done" or "error". */
    suspend fun loginBegin(phone: String): LoginState = io {
        call("login_begin", phone).asJsonObject().toLoginState()
    }

    suspend fun loginCode(code: String): LoginState = io {
        call("login_code", code).asJsonObject().toLoginState()
    }

    suspend fun loginPassword(password: String): LoginState = io {
        call("login_password", password).asJsonObject().toLoginState()
    }

    suspend fun logout(): Boolean = io {
        call("logout").asJsonObject().optBoolean("server_ok")
    }

    suspend fun resetEverything(): Boolean = io {
        call("reset_everything").toString() == "True"
    }

    // -- the rest ---------------------------------------------------------

    suspend fun dependencies(): List<DependencyRow> = io {
        call("get_dependencies").asJsonArray().mapObjects { o ->
            DependencyRow(
                name = o.optString("name"),
                ok = o.optBoolean("ok"),
                version = o.optString("version"),
                hint = o.optString("hint"),
                install = o.optString("install"),
                required = o.optBoolean("required"),
                applicable = o.optBoolean("applicable", true),
            )
        }
    }

    suspend fun checkForUpdate(): UpdateResult = io {
        call("check_for_update").asJsonObject().let { o ->
            UpdateResult(
                ok = o.optBoolean("ok"),
                version = o.optString("version"),
                url = o.optString("url"),
                current = o.optString("current"),
                newer = o.optBoolean("newer"),
                error = o.optString("error"),
            )
        }
    }

    /**
     * Start downloading the update package. Returns as soon as it is
     * under way -- the download itself is in the background, and its
     * progress is polled with updateDownloadState().
     */
    suspend fun startUpdateDownload(): UpdateResult = io {
        call("start_update_download").asJsonObject().let { o ->
            UpdateResult(
                ok = o.optBoolean("ok"),
                version = o.optString("version"),
                error = o.optString("error"),
            )
        }
    }

    /** The state of the update download. */
    suspend fun updateDownloadState(): UpdateDownloadState = io {
        call("get_update_download_state").asJsonObject().let { o ->
            UpdateDownloadState(
                status = o.optString("status", "idle"),
                progress = o.optDouble("progress", 0.0).toFloat(),
                done = o.optLong("done", 0L),
                total = o.optLong("total", 0L),
                detail = o.optString("detail"),
                error = o.optString("error"),
                path = o.optString("path"),
                version = o.optString("version"),
                name = o.optString("name"),
            )
        }
    }
}

/** The state of a login in progress. */
data class LoginState(
    val ok: Boolean = false,
    val state: String = "error",
    val error: String = "",
)

private fun JSONObject.toLoginState(): LoginState = LoginState(
    ok = optBoolean("ok"),
    state = optString("state", "error"),
    error = optString("error"),
)
