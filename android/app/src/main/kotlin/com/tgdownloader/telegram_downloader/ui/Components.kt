package com.tgdownloader.telegram_downloader.ui

import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.tgdownloader.telegram_downloader.Radius
import com.tgdownloader.telegram_downloader.Space
import com.tgdownloader.telegram_downloader.Type
import com.tgdownloader.telegram_downloader.tgColors

/**
 * The shared primitives -- the port of `gtk/widgets.py`, which is itself the
 * port of `telegram_downloader.ui`.
 *
 * Every colour here comes from the token palette; nothing in this file knows
 * a hex value, and nothing hardcodes a size that the spacing scale owns.
 */

/** A page: the page surface, the gutter, and a scroll. */
@Composable
fun TgPage(
    modifier: Modifier = Modifier,
    content: @Composable ColumnScope.() -> Unit,
) {
    Column(
        modifier = modifier
            .fillMaxSize()
            .background(tgColors().page)
            .verticalScroll(rememberScrollState())
            .padding(Space.pagePad),
        verticalArrangement = Arrangement.spacedBy(Space.s4),
        content = content,
    )
}

// ---------------------------------------------------------------------------
// buttons
// ---------------------------------------------------------------------------

enum class TgButtonKind { Primary, Secondary, Quiet, Danger }

/**
 * A labelled button.
 *
 * `kind`: "primary" (accent fill, the one affirmative action), "secondary"
 * (raised fill plus a hairline), "quiet" (no fill until touched) or
 * "danger" (error-tinted text).
 *
 * `busy` is the third rule made concrete: a button that awaits something
 * swaps its label for a spinner and is disabled while it runs, so a second
 * tap cannot start the same work twice.
 */
@Composable
fun TgButton(
    text: String,
    kind: TgButtonKind = TgButtonKind.Secondary,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    busy: Boolean = false,
    leading: (@Composable () -> Unit)? = null,
) {
    val c = tgColors()
    val shape = RoundedCornerShape(Radius.md)
    val background = when (kind) {
        TgButtonKind.Primary -> c.accent
        TgButtonKind.Secondary -> c.raised
        TgButtonKind.Quiet -> Color.Transparent
        TgButtonKind.Danger -> Color.Transparent
    }
    val foreground = when (kind) {
        TgButtonKind.Primary -> c.onAccent
        TgButtonKind.Secondary -> c.ink
        TgButtonKind.Quiet -> c.muted
        TgButtonKind.Danger -> c.error
    }
    val active = enabled && !busy
    Row(
        modifier = modifier
            .clip(shape)
            .background(background)
            .then(
                if (kind == TgButtonKind.Secondary) {
                    Modifier.border(1.dp, c.hairline, shape)
                } else {
                    Modifier
                }
            )
            .clickable(enabled = active) { onClick() }
            .padding(horizontal = Space.s3 + Space.s2, vertical = Space.s1 + Space.s3),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(Space.s2 - Space.s1),
    ) {
        if (busy) {
            CircularProgressIndicator(
                color = foreground,
                strokeWidth = 2.dp,
                modifier = Modifier.size(14.dp),
            )
        } else {
            leading?.invoke()
            Text(
                text = text,
                color = if (active) foreground else foreground.copy(alpha = 0.45f),
                fontSize = Type.body,
                fontWeight = FontWeight.SemiBold,
            )
        }
    }
}

// ---------------------------------------------------------------------------
// the segmented control
// ---------------------------------------------------------------------------

data class TgSegmentOption(
    val value: String,
    val label: String,
    val enabled: Boolean = true,
)

/**
 * The segmented control: one raised segment inside a trough.
 *
 * The selected segment is a RAISED FILL, not an accent fill. A saturated
 * block inside a form competes with the one primary button on the page and
 * reads as a status rather than a choice; mass says "selected" quietly.
 */
@Composable
fun TgSegmented(
    options: List<TgSegmentOption>,
    value: String,
    onChange: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    val c = tgColors()
    val shape = RoundedCornerShape(Radius.md)
    Row(
        modifier = modifier
            .background(c.well, shape)
            .padding(Space.s1),
        horizontalArrangement = Arrangement.spacedBy(Space.s1),
    ) {
        for (option in options) {
            val selected = option.value == value
            val segmentShape = RoundedCornerShape(Radius.sm)
            Box(
                modifier = Modifier
                    .clip(segmentShape)
                    .background(if (selected) c.raised else Color.Transparent)
                    .then(
                        if (selected) {
                            Modifier.border(1.dp, c.hairline, segmentShape)
                        } else {
                            Modifier
                        }
                    )
                    .clickable(enabled = option.enabled) { onChange(option.value) }
                    .padding(horizontal = Space.s3 + Space.s2, vertical = Space.s1),
                contentAlignment = Alignment.Center,
            ) {
                Text(
                    text = option.label,
                    color = when {
                        !option.enabled -> c.faint
                        selected -> c.ink
                        else -> c.muted
                    },
                    fontSize = Type.body,
                    fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Normal,
                )
            }
        }
    }
}

// ---------------------------------------------------------------------------
// marks
// ---------------------------------------------------------------------------

enum class TgTone { Neutral, Ok, Error }

/** A small count or tag. It sits on a wash of its own tone. */
@Composable
fun TgBadge(text: String, tone: TgTone = TgTone.Neutral) {
    val c = tgColors()
    val (background, foreground) = when (tone) {
        TgTone.Neutral -> c.muted.copy(alpha = 0.12f) to c.muted
        TgTone.Ok -> c.success.copy(alpha = 0.12f) to c.success
        TgTone.Error -> c.error.copy(alpha = 0.12f) to c.error
    }
    Box(
        modifier = Modifier
            .clip(RoundedCornerShape(Radius.sm))
            .background(background)
            .padding(horizontal = Space.s1 + Space.s2, vertical = 1.dp),
    ) {
        Text(
            text = text,
            color = foreground,
            fontSize = Type.micro,
            fontWeight = FontWeight.SemiBold,
        )
    }
}

/**
 * The coloured dot that leads a queue row.
 *
 * The only place a status colour is allowed to be a mark.
 */
@Composable
fun TgStatusDot(state: String, size: Dp = 9.dp) {
    val c = tgColors()
    val color = when (state) {
        "working" -> c.accent
        "paused" -> c.accent.copy(alpha = 0.55f)
        "done" -> c.success
        "failed" -> c.error
        "cancelled" -> c.outline
        else -> c.muted
    }
    Box(
        modifier = Modifier
            .size(size)
            .clip(CircleShape)
            .background(color),
    )
}

// ---------------------------------------------------------------------------
// text
// ---------------------------------------------------------------------------

/** A section heading: a muted label, and an optional trailing widget. */
@Composable
fun TgSectionLabel(text: String, trailing: (@Composable () -> Unit)? = null) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text(
            text = text,
            color = tgColors().muted,
            fontSize = Type.label,
            fontWeight = FontWeight.SemiBold,
            modifier = Modifier.weight(1f),
        )
        trailing?.invoke()
    }
}

enum class TgNoticeKind { Info, Error }

/**
 * An inline message about the thing it is about.
 *
 * A wash of the tone rather than a solid fill, so it reads as a message
 * attached to the page and not as a banner sliding in.
 */
@Composable
fun TgNotice(text: String, kind: TgNoticeKind = TgNoticeKind.Info) {
    val c = tgColors()
    val tone = if (kind == TgNoticeKind.Error) c.error else c.accent
    val shape = RoundedCornerShape(Radius.md)
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .clip(shape)
            .background(tone.copy(alpha = 0.10f))
            .border(1.dp, tone.copy(alpha = 0.35f), shape)
            .padding(horizontal = Space.s3, vertical = Space.s2 + Space.s2),
    ) {
        Text(text = text, color = c.ink, fontSize = Type.body)
    }
}

/** A status line: a dot and a sentence, the way the GTK app shows it. */
@Composable
fun TgStatusLine(text: String) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(Space.s2),
    ) {
        TgStatusDot("waiting", size = 7.dp)
        Text(text = text, color = tgColors().muted, fontSize = Type.meta)
    }
}

// ---------------------------------------------------------------------------
// fields
// ---------------------------------------------------------------------------

/** A labelled slot: a caption above, an optional hint opposite it. */
@Composable
fun TgFieldLabel(text: String, hint: String? = null) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text(
            text = text,
            color = tgColors().muted,
            fontSize = Type.label,
            fontWeight = FontWeight.SemiBold,
            modifier = Modifier.weight(1f),
        )
        if (hint != null) {
            Text(text = hint, color = tgColors().muted, fontSize = Type.meta)
        }
    }
}

/**
 * The text field: an input fill, an outline, an accent caret.
 *
 * `minLines` grows it (the link field is five lines tall), and the hint
 * steps aside when there is text.
 */
@Composable
fun TgTextField(
    value: String,
    onValueChange: (String) -> Unit,
    modifier: Modifier = Modifier,
    placeholder: String = "",
    singleLine: Boolean = true,
    minLines: Int = 1,
    isError: Boolean = false,
    password: Boolean = false,
) {
    val c = tgColors()
    val shape = RoundedCornerShape(Radius.md)
    var focused by remember { mutableStateOf(false) }
    val borderColor = when {
        isError -> c.error
        focused -> c.accent
        else -> c.outline
    }
    BasicTextField(
        value = value,
        onValueChange = onValueChange,
        textStyle = TextStyle(color = c.ink, fontSize = Type.body),
        cursorBrush = SolidColor(c.accent),
        singleLine = singleLine,
        minLines = if (singleLine) 1 else minLines,
        keyboardOptions = KeyboardOptions(
            keyboardType = if (password) KeyboardType.Password else KeyboardType.Text,
        ),
        visualTransformation = if (password) {
            PasswordVisualTransformation()
        } else {
            VisualTransformation.None
        },
        modifier = modifier
            .fillMaxWidth()
            .onFocusChanged { focused = it.isFocused }
            .background(c.wellDeep, shape)
            .border(1.dp, borderColor, shape)
            .padding(horizontal = Space.s3, vertical = Space.s2 + Space.s2),
        decorationBox = { inner ->
            Box(modifier = Modifier.fillMaxWidth()) {
                if (value.isEmpty() && placeholder.isNotEmpty()) {
                    Text(
                        text = placeholder,
                        color = c.faint,
                        fontSize = Type.body,
                        maxLines = if (singleLine) 1 else 3,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
                inner()
            }
        },
    )
}

// ---------------------------------------------------------------------------
// the progress bar
// ---------------------------------------------------------------------------

/**
 * The thin bar under a queue row.
 *
 * `value == null` is the indeterminate form: the bar animates while the size
 * is still unknown, which is the honest thing to show while a message is
 * being fetched.
 */
@Composable
fun TgProgressBar(value: Float?, modifier: Modifier = Modifier) {
    val c = tgColors()
    val shape = RoundedCornerShape(Radius.full)
    var width by remember { mutableStateOf(0) }
    val transition = rememberInfiniteTransition(label = "tg-progress")
    val phase by transition.animateFloat(
        initialValue = 0f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(tween(1400, easing = LinearEasing)),
        label = "tg-progress-phase",
    )
    Box(
        modifier = modifier
            .fillMaxWidth()
            .onSizeChanged { width = it.width }
            .height(4.dp)
            .background(c.wellDeep, shape),
    ) {
        val fraction = if (value == null) 0.3f else value.coerceIn(0f, 1f)
        val offset = if (value == null) (phase * 1.4f - 0.4f) * width else 0f
        Box(
            modifier = Modifier
                .fillMaxWidth(fraction)
                .graphicsLayer { translationX = offset.toFloat() }
                .background(c.accent, shape),
        )
    }
}

// ---------------------------------------------------------------------------
// small helpers
// ---------------------------------------------------------------------------

/** A 1dp hairline rule. */
@Composable
fun TgHairline(modifier: Modifier = Modifier, vertical: Boolean = false) {
    val c = tgColors()
    Box(
        modifier = modifier
            .then(if (vertical) Modifier.width(1.dp) else Modifier.fillMaxWidth())
            .then(if (vertical) Modifier.height(18.dp) else Modifier.height(1.dp))
            .background(c.hairline),
    )
}
