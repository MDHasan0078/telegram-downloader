# R8 rules for the native Android app.
#
# The Python engine is NOT in the DEX. It is in assets/chaquopy/*.imy --
# .py files and compiled extensions -- which R8 never sees, so there is
# nothing here to protect telethon from. The only things that need
# keeping are the ones R8 cannot see being used.

# Chaquopy's Java API. The bridge calls into it dynamically, so the
# classes and the members have to survive.
-keep class com.chaquo.python.** { *; }

# The bridge's own entry points: the Kotlin calls them by name, and the
# manifest names the two application classes.
-keep class com.tgdownloader.telegram_downloader.Bridge { *; }
-keep class com.tgdownloader.telegram_downloader.TgApplication { *; }
-keep class com.tgdownloader.telegram_downloader.MainActivity { *; }

# The log line that says the Python engine is ready is what the CI
# launch check greps for. It has to survive the shrink.
-keep class android.util.Log {
    public static *** v(...);
    public static *** d(...);
    public static *** i(...);
    public static *** w(...);
    public static *** e(...);
}

# Kotlin metadata is used by the Compose compiler's generated code.
-keepattributes *Annotation*, InnerClasses, Signature, RuntimeVisible*Annotations
