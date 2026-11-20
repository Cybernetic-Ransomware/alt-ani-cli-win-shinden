package pl.altani.spike.ui

import java.math.BigInteger
import java.util.Locale
import org.json.JSONException
import org.json.JSONObject

/** `best`, or `max:<height>`: an upper limit, never an exact resolution; same grammar as the engine's `QualityCriterion`. */
sealed interface Quality {
    fun encode(): String

    data object Best : Quality {
        override fun encode() = "best"
    }

    data class Max(val height: Int) : Quality {
        init {
            require(height in HEIGHTS) { "a quality limit must be a positive height" }
        }

        override fun encode() = "max:$height"
    }

    companion object {
        val HEIGHTS = 1..99_999
        private val MAX_RE = Regex("max:([1-9]\\d{0,4})")

        fun parse(text: String): Quality =
            if (text == "best") Best else MAX_RE.matchEntire(text)?.let { Max(it.groupValues[1].toInt()) } ?: throw IllegalArgumentException("unknown quality")
    }
}

enum class DownloadNetwork(val wire: String) {
    WIFI_ONLY("wifi_only"),
    ANY("any"),
    ;

    companion object {
        fun parse(wire: String): DownloadNetwork = entries.firstOrNull { it.wire == wire } ?: throw IllegalArgumentException("unknown download network")
    }
}

enum class PreferenceField(val wire: String) {
    AUDIO("audio"),
    SUBS("subs"),
    QUALITY("quality"),
    DOWNLOAD_NETWORK("download_network"),
    AUTO_ALTERNATIVES("auto_alternatives"),
    MAX_ACTIVE_DOWNLOADS("max_active_downloads"),
}

/** The six preferences as the engine stores them; languages are the sources' own labels, already normalised by the engine. */
data class PreferenceValues(
    val audio: String = ANY,
    val subs: String = ANY,
    val quality: Quality = Quality.Best,
    val downloadNetwork: DownloadNetwork = DownloadNetwork.WIFI_ONLY,
    val autoAlternatives: Boolean = false,
    val maxActiveDownloads: Int = 1,
) {
    init {
        require(normalizeLanguage(audio).isNotEmpty() && normalizeLanguage(audio) != NO_SUBTITLES) { "invalid audio criterion" }
        require(normalizeLanguage(subs).isNotEmpty()) { "invalid subtitle criterion" }
        // the engine allows any positive count; 16 is only the bridge's input limit, so a larger stored value is still shown
        require(maxActiveDownloads >= 1) { "at least one download must be allowed to run" }
    }

    companion object {
        const val ANY = "any"
        const val NO_SUBTITLES = "none"
    }
}

data class Preferences(val values: PreferenceValues, val revision: Long) {
    companion object {
        /** Every key required with its exact JSON type: no string for a flag, no fraction for a count, no unknown value. */
        fun parse(data: JSONObject): Preferences =
            try {
                Preferences(
                    PreferenceValues(
                        audio = data.strictString("audio"),
                        subs = data.strictString("subs"),
                        quality = Quality.parse(data.strictString("quality")),
                        downloadNetwork = DownloadNetwork.parse(data.strictString("download_network")),
                        autoAlternatives = data.strictBoolean("auto_alternatives"),
                        maxActiveDownloads = Math.toIntExact(data.strictLong("max_active_downloads")),
                    ),
                    data.strictLong("revision").also { if (it < 0) throw JSONException("revision is negative") },
                )
            } catch (e: IllegalArgumentException) {
                throw JSONException("invalid preferences")
            } catch (e: ArithmeticException) {
                throw JSONException("count out of range")
            }
    }
}

enum class LanguageMode { ANY, SPECIFIC }

enum class SubsMode { ANY, NONE, SPECIFIC }

enum class QualityMode { BEST, MAX }

/** The editable draft, raw text included, so a half-typed value survives a tab change; never sent until Save. */
data class SettingsForm(
    val audioMode: LanguageMode,
    val audioText: String,
    val subsMode: SubsMode,
    val subsText: String,
    val qualityMode: QualityMode,
    val heightText: String,
    val downloadNetwork: DownloadNetwork,
    val autoAlternatives: Boolean,
    val maxActiveDownloads: Int,
) {
    /** What the field asks for, comparable with another form's; an invalid draft still has one. */
    fun intent(field: PreferenceField): String =
        when (field) {
            PreferenceField.AUDIO -> if (audioMode == LanguageMode.ANY) PreferenceValues.ANY else normalizeLanguage(audioText)
            PreferenceField.SUBS -> when (subsMode) {
                SubsMode.ANY -> PreferenceValues.ANY
                SubsMode.NONE -> PreferenceValues.NO_SUBTITLES
                SubsMode.SPECIFIC -> normalizeLanguage(subsText)
            }
            PreferenceField.QUALITY -> if (qualityMode == QualityMode.BEST) "best" else "max:${heightText.trim()}"
            PreferenceField.DOWNLOAD_NETWORK -> downloadNetwork.wire
            PreferenceField.AUTO_ALTERNATIVES -> autoAlternatives.toString()
            PreferenceField.MAX_ACTIVE_DOWNLOADS -> maxActiveDownloads.toString()
        }

    fun take(field: PreferenceField, other: SettingsForm): SettingsForm =
        when (field) {
            PreferenceField.AUDIO -> copy(audioMode = other.audioMode, audioText = other.audioText)
            PreferenceField.SUBS -> copy(subsMode = other.subsMode, subsText = other.subsText)
            PreferenceField.QUALITY -> copy(qualityMode = other.qualityMode, heightText = other.heightText)
            PreferenceField.DOWNLOAD_NETWORK -> copy(downloadNetwork = other.downloadNetwork)
            PreferenceField.AUTO_ALTERNATIVES -> copy(autoAlternatives = other.autoAlternatives)
            PreferenceField.MAX_ACTIVE_DOWNLOADS -> copy(maxActiveDownloads = other.maxActiveDownloads)
        }

    fun dirty(base: PreferenceValues): Set<PreferenceField> {
        val saved = from(base)
        return PreferenceField.entries.filterTo(LinkedHashSet()) { intent(it) != saved.intent(it) }
    }

    fun problems(fields: Set<PreferenceField>): Set<PreferenceField> = fields.filterTo(LinkedHashSet()) { !valid(it) }

    private fun valid(field: PreferenceField): Boolean =
        when (field) {
            PreferenceField.AUDIO -> audioMode == LanguageMode.ANY || normalizeLanguage(audioText).let { it.isNotEmpty() && it != PreferenceValues.NO_SUBTITLES }
            PreferenceField.SUBS -> subsMode != SubsMode.SPECIFIC || normalizeLanguage(subsText).isNotEmpty()
            PreferenceField.QUALITY -> qualityMode == QualityMode.BEST || HEIGHT_RE.matches(heightText.trim())
            PreferenceField.DOWNLOAD_NETWORK, PreferenceField.AUTO_ALTERNATIVES -> true
            PreferenceField.MAX_ACTIVE_DOWNLOADS -> maxActiveDownloads in MAX_ACTIVE
        }

    /** Only the fields that differ from [base]; null while one of them is invalid. Languages go as typed, the engine normalises them. */
    fun changes(base: PreferenceValues): JSONObject? {
        val fields = dirty(base)
        if (problems(fields).isNotEmpty()) return null
        val json = JSONObject()
        for (field in fields) {
            when (field) {
                PreferenceField.AUDIO -> json.put(field.wire, if (audioMode == LanguageMode.ANY) PreferenceValues.ANY else audioText.trim())
                PreferenceField.SUBS -> json.put(
                    field.wire,
                    when (subsMode) {
                        SubsMode.ANY -> PreferenceValues.ANY
                        SubsMode.NONE -> PreferenceValues.NO_SUBTITLES
                        SubsMode.SPECIFIC -> subsText.trim()
                    },
                )
                PreferenceField.QUALITY -> json.put(field.wire, intent(field))
                PreferenceField.DOWNLOAD_NETWORK -> json.put(field.wire, downloadNetwork.wire)
                PreferenceField.AUTO_ALTERNATIVES -> json.put(field.wire, autoAlternatives)
                PreferenceField.MAX_ACTIVE_DOWNLOADS -> json.put(field.wire, maxActiveDownloads)
            }
        }
        return json
    }

    fun describe(field: PreferenceField): String =
        when (field) {
            PreferenceField.AUDIO -> if (audioMode == LanguageMode.ANY) "Any language" else audioText.trim()
            PreferenceField.SUBS -> when (subsMode) {
                SubsMode.ANY -> "Any (no preference)"
                SubsMode.NONE -> "None (no subtitles)"
                SubsMode.SPECIFIC -> subsText.trim()
            }
            PreferenceField.QUALITY -> if (qualityMode == QualityMode.BEST) "Best available" else "At most ${heightText.trim()}p"
            PreferenceField.DOWNLOAD_NETWORK -> if (downloadNetwork == DownloadNetwork.WIFI_ONLY) "Wi-Fi only" else "Wi-Fi and mobile data"
            PreferenceField.AUTO_ALTERNATIVES -> if (autoAlternatives) "On" else "Off"
            PreferenceField.MAX_ACTIVE_DOWNLOADS -> maxActiveDownloads.toString()
        }

    companion object {
        val MAX_ACTIVE = 1..16
        private val HEIGHT_RE = Regex("[1-9]\\d{0,4}")

        fun from(values: PreferenceValues): SettingsForm {
            val quality = values.quality
            return SettingsForm(
                audioMode = if (values.audio == PreferenceValues.ANY) LanguageMode.ANY else LanguageMode.SPECIFIC,
                audioText = if (values.audio == PreferenceValues.ANY) "" else values.audio,
                subsMode = when (values.subs) {
                    PreferenceValues.ANY -> SubsMode.ANY
                    PreferenceValues.NO_SUBTITLES -> SubsMode.NONE
                    else -> SubsMode.SPECIFIC
                },
                subsText = if (values.subs == PreferenceValues.ANY || values.subs == PreferenceValues.NO_SUBTITLES) "" else values.subs,
                qualityMode = if (quality is Quality.Max) QualityMode.MAX else QualityMode.BEST,
                heightText = if (quality is Quality.Max) quality.height.toString() else "",
                downloadNetwork = values.downloadNetwork,
                autoAlternatives = values.autoAlternatives,
                maxActiveDownloads = values.maxActiveDownloads,
            )
        }
    }
}

/** Close to the engine's `normalize_language` (whitespace collapsed, case folded); used for comparing, never sent. */
fun normalizeLanguage(value: String): String = value.trim().split(Regex("\\s+")).joinToString(" ").lowercase(Locale.ROOT)

private fun JSONObject.required(key: String): Any {
    if (!has(key) || isNull(key)) throw JSONException("$key is missing")
    return get(key)
}

private fun JSONObject.strictString(key: String): String = required(key) as? String ?: throw JSONException("$key is not a string")

private fun JSONObject.strictBoolean(key: String): Boolean = required(key) as? Boolean ?: throw JSONException("$key is not a boolean")

// org.json gives Integer, Long or BigInteger for a JSON integer and Double or BigDecimal for anything with a fraction or exponent
private fun JSONObject.strictLong(key: String): Long =
    when (val value = required(key)) {
        is Int -> value.toLong()
        is Long -> value
        is BigInteger -> try {
            value.longValueExact()
        } catch (e: ArithmeticException) {
            throw JSONException("$key is out of range")
        }
        else -> throw JSONException("$key is not an integer")
    }
