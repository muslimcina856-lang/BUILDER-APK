# Sokongan Godot Android

Builder ini menyediakan laluan eksport Android khusus untuk projek Godot tanpa mengubah aliran Flutter, Native Android, Smali atau React Native.

## Skop yang disokong

- Godot 3.x dan Godot 4.x GDScript menggunakan release stabil yang sepadan dengan `project.godot`.
- Godot 3.x C#/Mono menggunakan binary dan export templates Mono serta .NET SDK.
- Godot 4.2+ C#/.NET untuk Android menggunakan binary dan export templates .NET. Godot 4.0/4.1 C# Android tidak dipaksa kerana upstream belum menyokong eksport Android untuk C# pada versi itu.
- APK debug.
- APK release **unsigned**.
- AAB release **unsigned** melalui Gradle build. Dalam mode `auto`, `both`, atau `release`, builder sentiasa mencuba output AAB walaupun preset asal ditetapkan kepada APK.
- Lebih daripada satu Android export preset, termasuk pemilihan preset tertentu atau build semua preset.
- Pemasangan Android Gradle build template secara automatik apabila preset memerlukannya, tanpa menindih `android/build` custom yang sudah berisi.
- Debug keystore automatik untuk runner bersih.
- Pemeriksaan asas Android plugin dan GDExtension/GDNative supaya library Android yang hilang diberi amaran awal.

## Polisi signing V5 Direct Runner

Release signing oleh builder dimatikan sebagai polisi tetap.

- Keystore release yang berada dalam projek tidak digunakan sebagai identiti akhir.
- Secret atau environment release keystore user tidak diperlukan dan tidak dihantar oleh workflow.
- `auto` membina debug + release walaupun user tidak menyediakan keystore.
- `release` membina release sahaja.
- `both` membina debug + release.
- Semua APK/AAB release yang dihantar kepada user dinamakan dengan suffix `-unsigned`.
- User wajib sign sendiri selepas menerima artifact.

Sesetengah versi Godot memerlukan identiti signing semasa proses eksport release walaupun artifact akhirnya mahu disediakan unsigned. Untuk compatibility, runner menjana identiti sementara miliknya sendiri, menggunakannya hanya semasa proses packaging, kemudian membina semula artifact untuk membuang signature dan memadam keystore sementara tersebut. Identiti/keystore user tidak digunakan untuk proses ini.

Untuk APK, builder cuba menjalankan `zipalign` semula selepas signature dibuang supaya fail lebih bersedia untuk proses signing user. Untuk AAB, signature JAR/Gradle yang wujud dibuang daripada artifact akhir.

Debug APK kekal menggunakan debug keystore kerana ia ialah build debug, bukan artifact release untuk Play Store.

## Pilihan workflow

Input `workflow_dispatch`:

- `godot_preset`: nama Android export preset. Jika kosong, preset `runnable` digunakan dahulu, kemudian preset Android pertama.
- `godot_export_mode`: `auto`, `debug`, `release`, atau `both`.
- `godot_build_all_presets`: `true` untuk eksport semua Android preset.

Environment yang sama juga boleh digunakan apabila worker dijalankan di luar GitHub Actions:

```text
GODOT_PRESET
GODOT_EXPORT_MODE
GODOT_BUILD_ALL_PRESETS
```

Release keystore environment seperti `GODOT_RELEASE_KEYSTORE_*` tidak diperlukan untuk V5 Direct Runner.

## Android toolchain

Builder memilih JDK/SDK/Build Tools/NDK/CMake berdasarkan versi Godot. Contoh penting:

- Godot 3.2: Java 8.
- Godot 3.6.2+: Java 17, Android API 35, Build Tools 35.0.1, NDK 28.1.13356709.
- Godot 4.6/4.7: Java 17, Android API 35, Build Tools 35.0.1, NDK 28.1.13356709.
- Godot 4.8+: profile disediakan untuk API 36/Build Tools 36.1.0/NDK 29 apabila release stabil tersedia.

Godot 3 X11/Mono dijalankan melalui `xvfb-run` pada Linux CI. Workflow memastikan Xvfb tersedia.

## Gradle, AAB dan plugin

Jika preset menggunakan Gradle atau format AAB, builder memastikan `res://android/build` tersedia. Jika projek sudah mempunyai template custom yang sah, ia dikekalkan. Jika `android/build` wujud tetapi kandungannya tidak kelihatan seperti template Godot, build dihentikan daripada menindih fail custom.

Android plugin moden masih perlu dikonfigurasi dengan betul dalam preset projek. Builder tidak mengaktifkan plugin yang pengguna tidak pilih.

## GDExtension / GDNative

Prebuilt native extension boleh dieksport apabila descriptor projek mengisytiharkan library Android yang sesuai. Builder memberi amaran jika descriptor tidak mempunyai library Android atau fail `res://` yang dirujuk hilang.

Builder tidak cuba mengkompilasi source C/C++ arbitrary menjadi GDExtension/GDNative kerana setiap extension boleh mempunyai toolchain, SCons/CMake flags dan dependency tersendiri. Projek yang memerlukan compilation custom perlu menyertakan prebuilt Android libraries atau build step projek sendiri.

## Had yang disengajakan

- Laluan Godot dalam builder ini fokus kepada Android, bukan Windows/Linux/macOS/Web/iOS export.
- Prerelease/dev/RC Godot tidak dipilih secara automatik; resolver memilih stable release untuk reproducibility.
- Godot 4.0/4.1 C# Android ditolak dengan mesej jelas kerana sokongan upstream bermula pada 4.2.
- C# Android pada Godot 4.2+ masih tertakluk pada limitasi upstream Godot/.NET.
- Artifact release tidak boleh terus dihantar ke Google Play sebelum user menandatanganinya dengan upload/release key sendiri.

## Regression tests

`tests/test_godot_worker.py` meliputi version/toolchain mapping, Godot Standard vs Mono/.NET, multiple preset, APK/AAB, Gradle template, debug/release mode, polisi unsigned release, keystore user diabaikan, temporary signing identity dibuang, Godot 3 temporary bridge, pemulihan fail projek/preset, package ID fallback, native extension warning dan perbezaan CLI Godot 3 vs 4.

GitHub Actions menjalankan suite ini sebelum worker dimulakan.
