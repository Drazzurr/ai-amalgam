package com.drazzurr.amalgam;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageInstaller;
import android.os.Build;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * Keeps the app itself up to date without you doing anything.
 * Android lets an app replace itself without asking when you've allowed it to install apps
 * ("Install unknown apps") once. If Android still wants a confirmation, a notification asks
 * for one tap. It never updates while you're looking at the app.
 */
final class AppUpdater {
    private AppUpdater() {}

    static final String VERSION_URL = "https://raw.githubusercontent.com/Drazzurr/ai-amalgam/main/VERSION";
    static final String APK_URL = "https://github.com/Drazzurr/ai-amalgam/releases/download/android/AI-Amalgam.apk";
    private static final long CHECK_EVERY = 30 * 60 * 1000L;
    private static final String CH = "app_updates";
    private static final int ID_CONFIRM = 7;

    private static volatile boolean busy = false;
    private static volatile long nextAt = System.currentTimeMillis() + 60 * 1000L;   // let the app settle first

    static boolean allowed(Context c) {
        return c.getPackageManager().canRequestPackageInstalls();
    }

    /** Called every few seconds by the background service; does real work at most every half hour. */
    static void maybeUpdate(Context c) {
        long now = System.currentTimeMillis();
        if (busy || now < nextAt) return;
        if (!allowed(c)) {
            nextAt = now + CHECK_EVERY;
            return;
        }
        if (MainActivity.visible) {               // never pull the app out from under you
            nextAt = now + 60 * 1000L;
            return;
        }
        busy = true;
        Context app = c.getApplicationContext();
        new Thread(() -> {
            try {
                nextAt = System.currentTimeMillis() + run(app);
            } catch (Exception e) {
                log(app, "couldn't check: " + e.getClass().getSimpleName());
                nextAt = System.currentTimeMillis() + CHECK_EVERY;
            } finally {
                busy = false;
            }
        }, "app-updater").start();
    }

    /** Returns how long to wait before the next try. */
    private static long run(Context c) throws Exception {
        String current = BuildConfigVersion.name(c);
        String latest = new String(get(VERSION_URL + "?t=" + System.currentTimeMillis()), StandardCharsets.UTF_8).trim();
        if (!newer(latest, current)) return CHECK_EVERY;

        File apk = new File(c.getCacheDir(), "update.apk");
        download(APK_URL, apk);
        PackageInfo info = c.getPackageManager().getPackageArchiveInfo(apk.getPath(), 0);
        if (info == null || !c.getPackageName().equals(info.packageName) || !newer(info.versionName, current)) {
            // the new app is usually built a minute or two after the new version is published
            apk.delete();
            return 5 * 60 * 1000L;
        }
        if (MainActivity.visible) return 60 * 1000L;
        log(c, "installing " + info.versionName);
        install(c, apk);
        return CHECK_EVERY;
    }

    private static void install(Context c, File apk) throws Exception {
        PackageInstaller pi = c.getPackageManager().getPackageInstaller();
        PackageInstaller.SessionParams params = new PackageInstaller.SessionParams(
                PackageInstaller.SessionParams.MODE_FULL_INSTALL);
        params.setAppPackageName(c.getPackageName());
        if (Build.VERSION.SDK_INT >= 31) {
            params.setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED);
        }
        int id = pi.createSession(params);
        try (PackageInstaller.Session s = pi.openSession(id)) {
            try (InputStream in = new FileInputStream(apk);
                 OutputStream out = s.openWrite("base.apk", 0, apk.length())) {
                byte[] buf = new byte[64 * 1024];
                int n;
                while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
                s.fsync(out);
            }
            Intent done = new Intent(c, Done.class).setPackage(c.getPackageName());
            PendingIntent result = PendingIntent.getBroadcast(c, 7, done,
                    PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_MUTABLE);
            s.commit(result.getIntentSender());
        }
    }

    // ----- helpers -----

    static boolean newer(String a, String b) {
        if (a == null || b == null) return false;
        String[] x = a.trim().split("\\."), y = b.trim().split("\\.");
        for (int i = 0; i < Math.max(x.length, y.length); i++) {
            int p = i < x.length ? num(x[i]) : 0, q = i < y.length ? num(y[i]) : 0;
            if (p != q) return p > q;
        }
        return false;
    }

    private static int num(String s) {
        try {
            return Integer.parseInt(s.replaceAll("[^0-9]", ""));
        } catch (NumberFormatException e) {
            return 0;
        }
    }

    private static HttpURLConnection open(String url) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(15000);
        c.setReadTimeout(60000);
        c.setInstanceFollowRedirects(true);
        c.setUseCaches(false);
        if (c.getResponseCode() != 200) throw new java.io.IOException("HTTP " + c.getResponseCode());
        return c;
    }

    private static byte[] get(String url) throws Exception {
        HttpURLConnection c = open(url);
        try (InputStream in = c.getInputStream()) {
            return ProxyService.readAll(in);
        } finally {
            c.disconnect();
        }
    }

    private static void download(String url, File to) throws Exception {
        HttpURLConnection c = open(url);
        File part = new File(to.getPath() + ".part");
        try (InputStream in = c.getInputStream(); OutputStream out = new FileOutputStream(part)) {
            byte[] buf = new byte[64 * 1024];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
        } finally {
            c.disconnect();
        }
        if (!part.renameTo(to)) throw new java.io.IOException("couldn't save the download");
    }

    static void log(Context c, String what) {
        String t = new SimpleDateFormat("MMM d, h:mm a", Locale.US).format(new Date());
        ProxyService.prefs(c).edit().putString("app_update_log", t + ": " + what).apply();
    }

    static String lastLog(Context c) {
        return ProxyService.prefs(c).getString("app_update_log", "");
    }

    /** Android's answer about the install. */
    public static class Done extends BroadcastReceiver {
        @Override
        public void onReceive(Context c, Intent intent) {
            int status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, -999);
            if (status == PackageInstaller.STATUS_PENDING_USER_ACTION) {
                Intent confirm = Build.VERSION.SDK_INT >= 33
                        ? intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent.class)
                        : intent.getParcelableExtra(Intent.EXTRA_INTENT);
                if (confirm == null) return;
                log(c, "Android asked for a tap to finish the update");
                NotificationManager nm = c.getSystemService(NotificationManager.class);
                nm.createNotificationChannel(new NotificationChannel(CH, "App updates",
                        NotificationManager.IMPORTANCE_DEFAULT));
                PendingIntent tap = PendingIntent.getActivity(c, ID_CONFIRM,
                        confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                        PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
                nm.notify(ID_CONFIRM, new Notification.Builder(c, CH)
                        .setSmallIcon(android.R.drawable.stat_sys_download_done)
                        .setContentTitle("AI Amalgam update ready")
                        .setContentText("Tap to finish updating the app.")
                        .setContentIntent(tap)
                        .setAutoCancel(true)
                        .build());
            } else if (status == PackageInstaller.STATUS_SUCCESS) {
                log(c, "updated by itself");
            } else {
                String msg = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE);
                log(c, "update didn't install (" + (msg == null ? "status " + status : msg) + ")");
            }
        }
    }

    /** After the app updates itself (or the phone restarts): start the connection helper again. */
    public static class Started extends BroadcastReceiver {
        @Override
        public void onReceive(Context c, Intent intent) {
            String a = intent.getAction();
            if (Intent.ACTION_MY_PACKAGE_REPLACED.equals(a)) {
                log(c, "updated to " + BuildConfigVersion.name(c));
                c.getSystemService(NotificationManager.class).cancel(ID_CONFIRM);
            }
            try {
                c.startForegroundService(new Intent(c, ProxyService.class));
            } catch (Exception e) {
                // Android may refuse right after boot; the app starts it when you open it
            }
        }
    }
}
