package com.drazzurr.amalgam;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.os.IBinder;
import android.util.Base64;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URI;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Runs in the background while AI Amalgam is in use:
 *  1. A small, password-protected web proxy on port 8118. Android doesn't pass mobile data through
 *     to the Terminal's Linux system, but apps do have it - so Amalgam (inside Linux) sends its
 *     internet traffic through this app when it has no internet of its own.
 *     Only Amalgam knows the password, and only this phone's own addresses may connect.
 *  2. Watches Amalgam and posts a notification when a question is waiting or a discussion ends,
 *     even when the app is closed or the screen is off.
 */
public class ProxyService extends Service {
    static final int PORT = 8118;
    private static final String CH_RUNNING = "running";
    private static final String CH_UPDATES = "updates";
    private static final int ID_RUNNING = 1, ID_UPDATE = 2, ID_READY = 3;

    private static volatile String token;
    static volatile boolean wantReadyNotice = false;
    private static volatile ProxyService instance;

    private final ExecutorService pool = Executors.newCachedThreadPool();
    private volatile boolean stopping = false;
    private ServerSocket server;
    private volatile int connections = 0;

    // ----- shared settings -----

    static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences("amalgam", MODE_PRIVATE);
    }

    static String password(Context c) {
        SharedPreferences p = prefs(c);
        String pw = p.getString("proxy_password", null);
        if (pw == null) {
            byte[] b = new byte[18];
            new SecureRandom().nextBytes(b);
            pw = Base64.encodeToString(b, Base64.URL_SAFE | Base64.NO_WRAP | Base64.NO_PADDING);
            p.edit().putString("proxy_password", pw).apply();
        }
        return pw;
    }

    static void setToken(Context c, String t) {
        if (t == null || t.isEmpty()) return;
        token = t;
        prefs(c).edit().putString("token", t).apply();
    }

    // ----- service -----

    @Override
    public void onCreate() {
        super.onCreate();
        instance = this;
        NotificationManager nm = getSystemService(NotificationManager.class);
        NotificationChannel running = new NotificationChannel(CH_RUNNING, "Running in the background",
                NotificationManager.IMPORTANCE_MIN);
        running.setShowBadge(false);
        nm.createNotificationChannel(running);
        nm.createNotificationChannel(new NotificationChannel(CH_UPDATES, "Questions and finished discussions",
                NotificationManager.IMPORTANCE_HIGH));
        if (token == null) token = prefs(this).getString("token", null);
        password(this);

        startForeground(ID_RUNNING, runningNotice(), ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        pool.execute(this::serve);
        pool.execute(this::watch);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        stopping = true;
        instance = null;
        try {
            if (server != null) server.close();
        } catch (IOException ignored) {
        }
        pool.shutdownNow();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private PendingIntent openApp() {
        Intent i = new Intent(this, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        return PendingIntent.getActivity(this, 0, i, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    private Notification runningNotice() {
        return new Notification.Builder(this, CH_RUNNING)
                .setSmallIcon(android.R.drawable.stat_notify_sync_noanim)
                .setContentTitle("AI Amalgam")
                .setContentText("Keeping AI Amalgam connected")
                .setContentIntent(openApp())
                .setOngoing(true)
                .build();
    }

    private void notifyUser(int id, String text) {
        Notification n = new Notification.Builder(this, CH_UPDATES)
                .setSmallIcon(android.R.drawable.stat_notify_chat)
                .setContentTitle("AI Amalgam")
                .setContentText(text)
                .setContentIntent(openApp())
                .setAutoCancel(true)
                .build();
        getSystemService(NotificationManager.class).notify(id, n);
    }

    static void cancelReadyNotice(Context c) {
        c.getSystemService(NotificationManager.class).cancel(ID_READY);
        c.getSystemService(NotificationManager.class).cancel(ID_UPDATE);
    }

    // ----- watching Amalgam for news -----

    private void watch() {
        String last = null;
        boolean wasUp = MainActivity.amalgamUp();
        while (!stopping) {
            try {
                Thread.sleep(3000);
            } catch (InterruptedException e) {
                return;
            }
            boolean up = MainActivity.amalgamUp();
            if (up && !wasUp && wantReadyNotice && !MainActivity.visible) {
                wantReadyNotice = false;
                notifyUser(ID_READY, "AI Amalgam is running. Tap to open it.");
            }
            wasUp = up;
            if (!up || token == null) continue;
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(MainActivity.HOME + "api/state?lite=1&after=999999999")
                        .openConnection();
                c.setRequestProperty("X-Token", token);
                c.setConnectTimeout(2000);
                c.setReadTimeout(4000);
                if (c.getResponseCode() != 200) continue;
                JSONObject s;
                try (InputStream in = c.getInputStream()) {
                    s = new JSONObject(new String(ProxyService.readAll(in), StandardCharsets.UTF_8));
                }
                String status = s.optString("status", "idle");
                JSONObject code = s.optJSONObject("code");
                boolean command = code != null && !code.isNull("pending_command");
                if ("running".equals(last) && !"running".equals(status) && !MainActivity.visible) {
                    String hint = s.optString("hint", "");
                    notifyUser(ID_UPDATE, command ? "A command is waiting for your OK."
                            : "waiting".equals(status) ? "A question is waiting for your answer."
                            : "error".equals(hint) ? "A model couldn't reply. Tap to retry."
                            : "The discussion finished.");
                }
                last = status;
            } catch (Exception ignored) {
            }
        }
    }

    // ----- the proxy -----

    private void serve() {
        while (!stopping) {
            try {
                server = new ServerSocket();
                server.setReuseAddress(true);
                server.bind(new InetSocketAddress(PORT));
                while (!stopping) {
                    Socket client = server.accept();
                    pool.execute(() -> handle(client));
                }
            } catch (IOException e) {
                if (stopping) return;
                try {
                    Thread.sleep(3000);       // port busy for a moment: try again
                } catch (InterruptedException ie) {
                    return;
                }
            }
        }
    }

    private static boolean allowedClient(InetAddress a) {
        // this phone and its own Linux system only (never other devices on the internet)
        return a.isLoopbackAddress() || a.isSiteLocalAddress() || a.isLinkLocalAddress();
    }

    private void handle(Socket client) {
        connections++;
        Socket up = new Socket();
        try (Socket c = client) {
            c.setSoTimeout(30000);
            if (!allowedClient(c.getInetAddress())) return;
            InputStream in = c.getInputStream();
            OutputStream out = c.getOutputStream();
            byte[] head = readHead(in);
            if (head == null) return;
            String text = new String(head, StandardCharsets.ISO_8859_1);
            String[] lines = text.split("\r\n");
            String[] first = lines[0].split(" ");
            if (first.length < 3) return;
            Map<String, String> headers = new HashMap<>();
            for (int i = 1; i < lines.length; i++) {
                int k = lines[i].indexOf(':');
                if (k > 0) headers.put(lines[i].substring(0, k).trim().toLowerCase(Locale.ROOT), lines[i].substring(k + 1).trim());
            }
            String expect = "Basic " + Base64.encodeToString(("amalgam:" + password(this)).getBytes(StandardCharsets.UTF_8),
                    Base64.NO_WRAP);
            if (!expect.equals(headers.get("proxy-authorization"))) {
                out.write(("HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=\"amalgam\"\r\n"
                        + "Content-Length: 0\r\nConnection: close\r\n\r\n").getBytes(StandardCharsets.ISO_8859_1));
                return;
            }
            String method = first[0], target = first[1];
            if (method.equalsIgnoreCase("CONNECT")) {
                int colon = target.lastIndexOf(':');
                String host = colon > 0 ? target.substring(0, colon) : target;
                int port = colon > 0 ? Integer.parseInt(target.substring(colon + 1)) : 443;
                if (host.startsWith("[") && host.endsWith("]")) host = host.substring(1, host.length() - 1);
                try {
                    up.connect(new InetSocketAddress(host, port), 15000);
                } catch (IOException e) {
                    out.write("HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n".getBytes(StandardCharsets.ISO_8859_1));
                    return;
                }
                out.write("HTTP/1.1 200 Connection established\r\n\r\n".getBytes(StandardCharsets.ISO_8859_1));
                out.flush();
                pipe(c, up, new byte[0]);
            } else {
                // plain http://host/path request (some tools, e.g. apt): forward it without the proxy headers
                URI uri = URI.create(target);
                int port = uri.getPort() > 0 ? uri.getPort() : 80;
                String path = (uri.getRawPath() == null || uri.getRawPath().isEmpty() ? "/" : uri.getRawPath())
                        + (uri.getRawQuery() != null ? "?" + uri.getRawQuery() : "");
                StringBuilder fwd = new StringBuilder(method + " " + path + " " + first[2] + "\r\n");
                for (int i = 1; i < lines.length; i++) {
                    String low = lines[i].toLowerCase(Locale.ROOT);
                    if (lines[i].isEmpty() || low.startsWith("proxy-") || low.startsWith("connection:")) continue;
                    fwd.append(lines[i]).append("\r\n");
                }
                fwd.append("Connection: close\r\n\r\n");
                try {
                    up.connect(new InetSocketAddress(uri.getHost(), port), 15000);
                } catch (IOException e) {
                    out.write("HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n".getBytes(StandardCharsets.ISO_8859_1));
                    return;
                }
                pipe(c, up, fwd.toString().getBytes(StandardCharsets.ISO_8859_1));
            }
        } catch (Exception ignored) {
        } finally {
            connections--;
            try {
                up.close();
            } catch (IOException ignored) {
            }
        }
    }

    static byte[] readAll(InputStream in) throws IOException {
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        byte[] b = new byte[16384];
        int n;
        while ((n = in.read(b)) >= 0) buf.write(b, 0, n);
        return buf.toByteArray();
    }

    /** Reads up to the blank line that ends the request head. */
    private static byte[] readHead(InputStream in) throws IOException {
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        int state = 0;
        while (buf.size() < 32768) {
            int b = in.read();
            if (b < 0) return null;
            buf.write(b);
            state = (b == '\r' && (state == 0 || state == 2)) ? state + 1
                    : (b == '\n' && (state == 1 || state == 3)) ? state + 1 : 0;
            if (state == 4) {
                byte[] all = buf.toByteArray();
                byte[] head = new byte[all.length - 4];
                System.arraycopy(all, 0, head, 0, head.length);
                return head;
            }
        }
        return null;
    }

    /** Copies bytes both ways until either side closes. */
    private void pipe(Socket client, Socket up, byte[] firstToUpstream) throws IOException {
        up.setSoTimeout(0);
        client.setSoTimeout(0);
        OutputStream upOut = up.getOutputStream();
        if (firstToUpstream.length > 0) {
            upOut.write(firstToUpstream);
            upOut.flush();
        }
        Thread back = new Thread(() -> copy(up, client));
        back.start();
        copy(client, up);
        try {
            back.join(2000);
        } catch (InterruptedException ignored) {
        }
        up.close();
    }

    private static void copy(Socket from, Socket to) {
        byte[] b = new byte[32768];
        try {
            InputStream in = from.getInputStream();
            OutputStream out = to.getOutputStream();
            int n;
            while ((n = in.read(b)) >= 0) {
                out.write(b, 0, n);
                out.flush();
            }
        } catch (IOException ignored) {
        } finally {
            try {
                to.shutdownOutput();
            } catch (IOException ignored) {
            }
        }
    }
}
