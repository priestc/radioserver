# kotlinx.serialization
-keepattributes *Annotation*, InnerClasses
-dontnote kotlinx.serialization.AnnotationsKt

-keepclassmembers class kotlinx.serialization.json.** {
    *** Companion;
}
-keepclasseswithmembers class kotlinx.serialization.json.** {
    kotlinx.serialization.KSerializer serializer(...);
}

-keep,includedescriptorclasses class com.example.radioclient.**$$serializer { *; }
-keepclassmembers class com.example.radioclient.** {
    *** Companion;
}
-keepclasseswithmembers class com.example.radioclient.** {
    kotlinx.serialization.KSerializer serializer(...);
}
