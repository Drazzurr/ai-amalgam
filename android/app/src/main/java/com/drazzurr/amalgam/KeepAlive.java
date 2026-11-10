package com.drazzurr.amalgam;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.RemoteInput;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;

import org.bouncycastle.asn1.x500.X500Name;
import org.bouncycastle.cert.X509v3CertificateBuilder;
import org.bouncycastle.cert.jcajce.JcaX509CertificateConverter;
import org.bouncycastle.cert.jcajce.JcaX509v3CertificateBuilder;
import org.bouncycastle.operator.ContentSigner;
import org.bouncycastle.operator.jcajce.JcaContentSignerBuilder;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.math.BigInteger;
import java.net.InetAddress;
import java.security.KeyFactory;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.PrivateKey;
import java.security.cert.Certificate;
import java.security.cert.CertificateFactory;
import java.security.spec.PKCS8EncodedKeySpec;
import java.util.Date;

import io.github.muntashirakon.adb.AbsAdbConnectionManager;
import io.github.muntashirakon.adb.AdbStream;
import io.github.muntashirakon.adb.android.AdbMdns;

/**
 * One-time "keep running" setup, done on the phone itself (no computer):
 * pairs with Android's Wireless debugging (like Shizuku does), then asks Android to let the
 * Terminal app (which runs AI Amalgam's Linux system) and this app keep running in the
 * background, and allows auto-return. Afterwards Wireless debugging can be switched off again.
 */
final class KeepAlive {
    private KeepAlive() {}

    static final String TERMINAL = MainActivity.TERMINAL_PACKAGE;
    private static final String CHANNEL = "setup";
    private static final int ID = 4;
    private static final String KEY_CODE = "code";
    static final String ACTION_CODE = "com.drazzurr.amalgam.PAIRING_CODE";

    private static volatile int pairingPort = -1;
    private static volatile InetAddress pairingHost;
    private static AdbMdns discovery;
    static volatile String status = "";      // shown in the app

    static String[] commands(Context c) {
        String me = c.getPackageName();
        return new String[]{
                "cmd deviceidle whitelist +" + TERMINAL,
                "cmd deviceidle whitelist +" + me,
                "am set-standby-bucket " + TERMINAL + " active",
                "am set-standby-bucket " + me + " active",
                "cmd appops set " + TERMINAL + " RUN_IN_BACKGROUND allow",
                "cmd appops set " + TERMINAL + " RUN_ANY_IN_BACKGROUND allow",
                "cmd appops set " + me + " RUN_ANY_IN_BACKGROUND allow",
                "cmd appops set " + me + " SYSTEM_ALERT_WINDOW allow",
                "cmd appops set " + me + " REQUEST_INSTALL_PACKAGES allow",
        };
    }

    static boolean done(Context c) {
        return ProxyService.prefs(c).getLong("keepalive_done", 0) > 0;
    }

    // ----- step 1: look for the pairing screen and ask for the code -----

    static synchronized void start(Context c) {
        Context app = c.getApplicationContext();
        status = "waiting";
        pairingPort = -1;
        if (discovery != null) discovery.stop();
        discovery = new AdbMdns(app, AdbMdns.SERVICE_TYPE_TLS_PAIRING, (host, port) -> {
            if (port > 0) {
                pairingPort = port;
                pairingHost = host;
            }
        });
        discovery.start();
        askForCode(app, "Wireless debugging → Pair device with pairing code. Then pull this down and type the 6-digit code.");
        Intent dev = new Intent(Settings.ACTION_APPLICATION_DEVELOPMENT_SETTINGS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        try {
            app.startActivity(dev);
        } catch (Exception ignored) {
        }
    }

    private static void askForCode(Context c, String text) {
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel(CHANNEL, "One-time setup", NotificationManager.IMPORTANCE_HIGH));
        RemoteInput input = new RemoteInput.Builder(KEY_CODE).setLabel("6-digit pairing code").build();
        Intent i = new Intent(ACTION_CODE).setClass(c, CodeReceiver.class);
        PendingIntent pi = PendingIntent.getBroadcast(c, 9, i, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_MUTABLE);
        Notification.Action action = new Notification.Action.Builder(
                android.graphics.drawable.Icon.createWithResource(c, android.R.drawable.ic_menu_edit), "Enter code", pi)
                .addRemoteInput(input).build();
        Notification n = new Notification.Builder(c, CHANNEL)
                .setSmallIcon(android.R.drawable.ic_lock_idle_lock)
                .setContentTitle("AI Amalgam: keep running setup")
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .addAction(action)
                .setOngoing(true)
                .build();
        nm.notify(ID, n);
    }

    private static void finish(Context c, String text, boolean ok) {
        status = ok ? "done" : "failed: " + text;
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        Notification n = new Notification.Builder(c, CHANNEL)
                .setSmallIcon(ok ? android.R.drawable.checkbox_on_background : android.R.drawable.stat_notify_error)
                .setContentTitle(ok ? "AI Amalgam will keep running" : "Keep-running setup didn't finish")
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setAutoCancel(true)
                .build();
        nm.notify(ID, n);
        if (discovery != null) {
            discovery.stop();
            discovery = null;
        }
    }

    // ----- step 2: the code arrives from the notification -----

    public static class CodeReceiver extends BroadcastReceiver {
        @Override
        public void onReceive(Context context, Intent intent) {
            Bundle results = RemoteInput.getResultsFromIntent(intent);
            CharSequence typed = results == null ? null : results.getCharSequence(KEY_CODE);
            String code = typed == null ? "" : typed.toString().replaceAll("\\D", "");
            Context app = context.getApplicationContext();
            if (code.length() != 6) {
                askForCode(app, "That wasn't 6 digits. Type the code shown under \"Pair device with pairing code\".");
                return;
            }
            askForCode(app, "Pairing…");
            new Thread(() -> run(app, code)).start();
        }
    }

    private static void run(Context c, String code) {
        try {
            for (int i = 0; i < 20 && pairingPort <= 0; i++) Thread.sleep(500);
            if (pairingPort <= 0) {
                askForCode(c, "Couldn't find the pairing screen. Keep \"Pair device with pairing code\" open and enter the code again.");
                return;
            }
            Adb adb = Adb.get(c);
            String host = pairingHost != null ? pairingHost.getHostAddress() : "127.0.0.1";
            try {
                adb.pair(host, pairingPort, code);
            } catch (Exception first) {
                adb.pair("127.0.0.1", pairingPort, code);
            }
            if (!adb.autoConnect(c, 15000)) {
                finish(c, "Paired, but couldn't connect. Make sure Wireless debugging is still on, then try again.", false);
                return;
            }
            StringBuilder log = new StringBuilder();
            for (String cmd : commands(c)) {
                log.append("$ ").append(cmd).append('\n').append(shell(adb, cmd)).append('\n');
            }
            try {
                adb.disconnect();
            } catch (Exception ignored) {
            }
            ProxyService.prefs(c).edit().putLong("keepalive_done", System.currentTimeMillis())
                    .putString("keepalive_log", log.toString()).apply();
            finish(c, "Done. Android will now let the Terminal and AI Amalgam keep running in the background. "
                    + "You can switch Wireless debugging off again.", true);
        } catch (Exception e) {
            String msg = e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage();
            finish(c, "Pairing failed (" + msg + "). Check the code and try again from Info → Phone app.", false);
        }
    }

    private static String shell(Adb adb, String cmd) throws Exception {
        try (AdbStream s = adb.openStream("shell:" + cmd); InputStream in = s.openInputStream()) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] b = new byte[4096];
            int n;
            while ((n = in.read(b)) > 0) out.write(b, 0, n);
            return out.toString("UTF-8").trim();
        } catch (java.io.IOException closed) {
            return "";
        }
    }

    /** ADB connection to this phone, with a key made once and kept in the app's private storage. */
    static final class Adb extends AbsAdbConnectionManager {
        private static Adb instance;
        private final PrivateKey key;
        private final Certificate cert;

        static synchronized Adb get(Context c) throws Exception {
            if (instance == null) instance = new Adb(c);
            return instance;
        }

        private Adb(Context c) throws Exception {
            setApi(Build.VERSION.SDK_INT);
            File kf = new File(c.getFilesDir(), "adb.key"), cf = new File(c.getFilesDir(), "adb.crt");
            if (kf.isFile() && cf.isFile()) {
                key = KeyFactory.getInstance("RSA").generatePrivate(new PKCS8EncodedKeySpec(read(kf)));
                cert = CertificateFactory.getInstance("X.509").generateCertificate(new ByteArrayInputStream(read(cf)));
            } else {
                KeyPairGenerator g = KeyPairGenerator.getInstance("RSA");
                g.initialize(2048);
                KeyPair kp = g.generateKeyPair();
                X500Name name = new X500Name("CN=AI Amalgam");
                long now = System.currentTimeMillis();
                X509v3CertificateBuilder b = new JcaX509v3CertificateBuilder(name, BigInteger.valueOf(now),
                        new Date(now - 86400000L), new Date(now + 3650L * 86400000L), name, kp.getPublic());
                ContentSigner signer = new JcaContentSignerBuilder("SHA256withRSA").build(kp.getPrivate());
                key = kp.getPrivate();
                cert = new JcaX509CertificateConverter().getCertificate(b.build(signer));
                write(kf, key.getEncoded());
                write(cf, cert.getEncoded());
            }
        }

        private static byte[] read(File f) throws Exception {
            try (FileInputStream in = new FileInputStream(f)) {
                return ProxyService.readAll(in);
            }
        }

        private static void write(File f, byte[] data) throws Exception {
            try (FileOutputStream out = new FileOutputStream(f)) {
                out.write(data);
            }
        }

        @Override
        protected PrivateKey getPrivateKey() {
            return key;
        }

        @Override
        protected Certificate getCertificate() {
            return cert;
        }

        @Override
        protected String getDeviceName() {
            return "AI Amalgam";
        }
    }
}
