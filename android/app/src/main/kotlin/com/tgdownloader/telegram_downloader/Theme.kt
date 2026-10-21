package com.tgdownloader.telegram_downloader

import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * The design tokens, mirrored from `src/telegram_downloader/gtk/tokens.py`.
 *
 * The desktop app's tokens are the source of truth: the GTK stylesheet is
 * BUILT from them (`gtk/theme.py`), so mirroring the numbers here keeps
 * the two apps the same app instead of two apps that drifted apart.
 *
 * The three rules the palette carries survive the port:
 *
 *  1. ONE ACCENT, AND IT MEANS ACTION. `accent` marks a thing you can DO --
 *     the primary button, a progress bar, a focus ring. It never marks
 *     where you are: the selected destination is a RAISED surface with a
 *     hairline, the same material as the selected segment of a segmented
 *     control.
 *  2. MATERIALS, NOT BOXES. `page` is the window, `well`/`wellDeep` are
 *     BELOW it (input fills, troughs) and `raised`/`floating` are ABOVE it
 *     (the selected chip, dialogs). Every surface is a tonal step, never a
 *     shadowed card.
 *  3. THE MEANING INVERTS CLEANLY. "Well" is always lower than the page and
 *     "raised" is always higher, in both themes.
 *
 * The contrast ratios are documented per token in ui.py and are already
 * checked: ink 14.5:1 on the page, muted 8.3:1, accent 9.0:1, outline
 * 3.44:1. Keep them.
 */
@Immutable
data class TgColors(
    // -- surfaces, deepest first
    val wellDeep: Color,
    val well: Color,
    val page: Color,
    val raised: Color,
    val floating: Color,
    // -- lines
    val hairline: Color,
    val outline: Color,
    val edge: Color,
    // -- text
    val ink: Color,
    val muted: Color,
    val faint: Color,
    // -- accent (cyan-aqua: Telegram's blue family, one step warmer)
    val accent: Color,
    val accentHover: Color,
    val onAccent: Color,
    val accentWash: Color,
    val onAccentWash: Color,
    // -- done (the only green in the app: a finished download)
    val success: Color,
    val onSuccess: Color,
    val successWash: Color,
    val onSuccessWash: Color,
    // -- error (download failed, reset confirmed)
    val error: Color,
    val onError: Color,
    val errorWash: Color,
    val onErrorWash: Color,
    // -- Android-only: elevation shadow and the scrim behind a modal.
    //    GTK CSS has neither (the GTK design is tonal steps, not drop
    //    shadows), so these exist only in this palette.
    val shadow: Color,
    val scrim: Color,
)

/** The palette, one entry per mode. Dark is the one the app ships in. */
object TgPalette {
    // BEGIN GENERATED palette
    val Dark = TgColors(
        wellDeep = Color(0xFF0F1318),
        well = Color(0xFF161B20),
        page = Color(0xFF1D2227),
        raised = Color(0xFF282D32),
        floating = Color(0xFF34383E),
        hairline = Color(0xFFFFFFFF).copy(alpha = 0.09f),
        outline = Color(0xFF6E757D),
        edge = Color(0xFFFFFFFF).copy(alpha = 0.14f),
        ink = Color(0xFFF1F4F6),
        muted = Color(0xFFB4BBC3),
        faint = Color(0xFF9199A2),
        accent = Color(0xFF30D7D9),
        accentHover = Color(0xFF67EAEB),
        onAccent = Color(0xFF002428),
        accentWash = Color(0xFF043B3D),
        onAccentWash = Color(0xFF86EBEC),
        success = Color(0xFF62D286),
        onSuccess = Color(0xFF0A2312),
        successWash = Color(0xFF163620),
        onSuccessWash = Color(0xFF9FE9B2),
        error = Color(0xFFF25451),
        onError = Color(0xFF311210),
        errorWash = Color(0xFF46221F),
        onErrorWash = Color(0xFFFFB9B1),
        shadow = Color(0xFF000000),
        scrim = Color(0xFF000000).copy(alpha = 0.60f),
    )

    val Light = TgColors(
        wellDeep = Color(0xFFE3E8EC),
        well = Color(0xFFECEFF3),
        page = Color(0xFFF6F8FA),
        raised = Color(0xFFFFFFFF),
        floating = Color(0xFFFFFFFF),
        hairline = Color(0xFF000000).copy(alpha = 0.09f),
        outline = Color(0xFF878D93),
        edge = Color(0xFF000000).copy(alpha = 0.06f),
        ink = Color(0xFF1A1F24),
        muted = Color(0xFF545C64),
        faint = Color(0xFF7A8188),
        accent = Color(0xFF007A87),
        accentHover = Color(0xFF006572),
        onAccent = Color(0xFFFFFFFF),
        accentWash = Color(0xFFC4F0F2),
        onAccentWash = Color(0xFF005A64),
        success = Color(0xFF067E3F),
        onSuccess = Color(0xFFFFFFFF),
        successWash = Color(0xFFD3F1D9),
        onSuccessWash = Color(0xFF0A562B),
        error = Color(0xFFBE222A),
        onError = Color(0xFFFFFFFF),
        errorWash = Color(0xFFFFE0DC),
        onErrorWash = Color(0xFF90101A),
        shadow = Color(0xFF000000),
        scrim = Color(0xFF000000).copy(alpha = 0.35f),
    )
    // END GENERATED palette
}

/**
 * Space: 4-based with an 8 anchor, in two tiers -- 4/8/12 inside a
 * component, 16/24/32 between blocks.
 */
object Space {
    // BEGIN GENERATED space
    val s1: Dp = 4.dp
    val s2: Dp = 8.dp
    val s3: Dp = 12.dp
    val s4: Dp = 16.dp
    val s5: Dp = 20.dp
    val s6: Dp = 24.dp
    val s7: Dp = 32.dp
    val s8: Dp = 40.dp

    val pagePad: Dp = 24.dp
    val barHeight: Dp = 52.dp
    val rowPadH: Dp = 12.dp
    val rowPadV: Dp = 10.dp
    // END GENERATED space
}

/**
 * Three radii with visible separation (6 / 10 / 16), scaled to the object
 * they are on: a control inside a row, a panel or button, then a floating
 * sheet.
 */
object Radius {
    // BEGIN GENERATED radius
    val sm: Dp = 6.dp
    val md: Dp = 10.dp
    val lg: Dp = 16.dp
    val full: Dp = 999.dp
    // END GENERATED radius
}

/**
 * Six sizes, 10.5 to 14.5 in 1px steps plus one jump. The steps are
 * deliberately small: the queue is a long list, and a 2px jump between a
 * row title and its status line shouts where the eye only needs a nudge.
 */
object Type {
    // BEGIN GENERATED type
    val micro = 10.5.sp // badges, chips
    val meta = 11.5.sp  // status lines, captions, field hints
    val label = 12.5.sp // section labels, field labels
    val body = 13.5.sp  // base: body copy, buttons, nav, inputs, values
    val title = 14.5.sp // row titles, the wordmark, dialog titles
    val display = 22.sp // empty-state headline
    // END GENERATED type
}

/** The palette in force for the tree below this point. */
val LocalTgColors = staticCompositionLocalOf { TgPalette.Dark }

/** The tokens in force for the tree below this point. */
@Composable
fun tgColors(): TgColors = LocalTgColors.current

/** Installs one palette for the tree below it. */
@Composable
fun TgTheme(dark: Boolean = true, content: @Composable () -> Unit) {
    CompositionLocalProvider(LocalTgColors provides if (dark) TgPalette.Dark else TgPalette.Light) {
        content()
    }
}
