package com.drazzurr.amalgam;

import android.Manifest;
import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.Message;
import android.provider.Settings;
import android.view.View;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import java.io.InputStream;
import java.net.InetSocketAddress;
import java.net.URL;
import java.net.HttpURLConnection;
import java.net.Socket;
import java.nio.charset.StandardCharsets;

/**
 * AI Amalgam for Android: shows the Amalgam app that runs inside the phone's Linux Terminal,
 * opens the Terminal for you when Amalgam isn't running yet, and keeps the connection helper
 * (ProxyService) running so the Linux system has internet on mobile data too.
 */
public class MainActivity extends Activity {
    static final String HOME = "http://127.0.0.1:8765/";
    static final String TERMINAL_PACKAGE = "com.android.virtualization.terminal";

    private WebView web;
    private ValueCallback<Uri[]> fileCallback;
    private boolean showingStart = false;
    private long terminalOpenedAt = 0;
    static volatile boolean visible = false;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 7);
        }
        startForegroundService(new Intent(this, ProxyService.class));

        web = new WebView(this);
        web.setBackgroundColor(0xFF1A1B1E);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setSupportMultipleWindows(true);
        s.setJavaScriptCanOpenWindowsAutomatically(true);
        s.setAllowFileAccess(false);
        s.setUserAgentString(s.getUserAgentString() + " AIAmalgamApp/" + BuildConfigVersion.name(this));
        web.addJavascriptInterface(new Bridge(), "AmalgamApp");

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                return openOutside(req.getUrl());
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
                if (req.isForMainFrame() && !showingStart) showStart(true);
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> cb, FileChooserParams params) {
                if (fileCallback != null) fileCallback.onReceiveValue(null);
                fileCallback = cb;
                try {
                    Intent pick = params.createIntent();
                    pick.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                    startActivityForResult(pick, 41);
                } catch (ActivityNotFoundException e) {
                    fileCallback = null;
                    return false;
                }
                return true;
            }

            @Override
            public boolean onCreateWindow(WebView view, boolean dialog, boolean gesture, Message result) {
                // window.open(link): open it in the phone's browser
                WebView probe = new WebView(MainActivity.this);
                probe.setWebViewClient(new WebViewClient() {
                    @Override
                    public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                        openOutside(r.getUrl());
                        v.destroy();
                        return true;
                    }
                });
                ((WebView.WebViewTransport) result.obj).setWebView(probe);
                result.sendToTarget();
                return true;
            }
        });
        setContentView(web);
        web.setSystemUiVisibility(View.SYSTEM_UI_FLAG_LAYOUT_STABLE);
        loadOrStart();
    }

    /** true = handled outside the app (links to other sites, the Terminal app) */
    private boolean openOutside(Uri uri) {
        String host = uri.getHost() == null ? "" : uri.getHost();
        String scheme = uri.getScheme() == null ? "" : uri.getScheme();
        if (scheme.equals("intent")) { openTerminal(); return true; }
        if ((scheme.equals("http") || scheme.equals("https")) && (host.equals("127.0.0.1") || host.equals("localhost"))) {
            return false;
        }
        if (scheme.equals("file") || scheme.equals("about") || scheme.equals("data") || scheme.equals("blob")) return false;
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
        } catch (ActivityNotFoundException ignored) {
        }
        return true;
    }

    /** 0 = nothing on the port yet (Linux still starting), 1 = the port answers but AI Amalgam
     *  doesn't yet (the Terminal forwards the port as soon as Linux boots), 2 = AI Amalgam is up. */
    static int amalgamStage() {
        try (Socket sock = new Socket()) {
            sock.connect(new InetSocketAddress("127.0.0.1", 8765), 700);
        } catch (Exception e) {
            return 0;
        }
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(HOME + "manifest.webmanifest").openConnection();
            c.setConnectTimeout(1000);
            c.setReadTimeout(2000);
            c.setUseCaches(false);
            if (c.getResponseCode() != 200) return 1;
            try (InputStream in = c.getInputStream()) {
                return new String(ProxyService.readAll(in), StandardCharsets.UTF_8).contains("AI Amalgam") ? 2 : 1;
            }
        } catch (Exception e) {
            return 1;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    static boolean amalgamUp() {
        return amalgamStage() == 2;
    }

    private void loadOrStart() {
        new Thread(() -> {
            int stage = amalgamStage();
            runOnUiThread(() -> {
                if (stage == 2) goHome();
                else showStart(stage == 0);     // Linux already up: just wait, no Terminal needed
            });
        }).start();
    }

    /** The page lost AI Amalgam (or you came back and it's gone): show the loading screen and,
     *  if Linux stopped, start it again quietly. Checks twice so a brief blip doesn't count. */
    void reconnect() {
        if (showingStart) return;
        new Thread(() -> {
            if (amalgamStage() == 2) return;
            try {
                Thread.sleep(1500);
            } catch (InterruptedException e) {
                return;
            }
            int stage = amalgamStage();
            if (stage == 2) return;
            runOnUiThread(() -> {
                if (showingStart) return;
                // keep anything you were typing; the page puts it back once AI Amalgam is up again
                web.evaluateJavascript("try{localStorage.setItem('amalgam_draft',"
                        + "document.getElementById('input').value)}catch(e){}", v -> showStart(stage == 0));
            });
        }).start();
    }

    private long startShownAt = 0;    // when the loading screen first appeared, to time the start

    private void goHome() {
        if (showingStart && startShownAt > 0) {
            ProxyService.prefs(this).edit()
                    .putLong("last_start_ms", System.currentTimeMillis() - startShownAt).apply();
        }
        startShownAt = 0;
        showingStart = false;
        web.loadUrl(HOME);
    }

    /** "Starting AI Amalgam" screen; opens the Terminal app (which starts it) once. */
    private void showStart(boolean openTerminalNow) {
        if (!showingStart || startShownAt == 0) startShownAt = System.currentTimeMillis();
        showingStart = true;
        String html;
        try (InputStream in = getAssets().open("starting.html")) {
            html = new String(ProxyService.readAll(in), StandardCharsets.UTF_8);
        } catch (Exception e) {
            html = "<body style='background:#1a1b1e;color:#eee;font-family:sans-serif'>Starting AI Amalgam...</body>";
        }
        web.loadDataWithBaseURL("file:///android_asset/", html, "text/html", "utf-8", null);
        boolean asked = ProxyService.prefs(this).getBoolean("asked_auto_return", false);
        if (openTerminalNow && (Settings.canDrawOverlays(this) || asked)
                && System.currentTimeMillis() - terminalOpenedAt > 60000) {
            attempts = 0;
            new Handler(Looper.getMainLooper()).postDelayed(() -> startLinux(5000), 300);
        }
    }

    private int attempts = 0;
    private static final Handler ui = new Handler(Looper.getMainLooper());

    /** Start Linux without leaving this screen: Android only lets the Terminal start it while the
     *  Terminal is on screen, so it opens for a few seconds underneath our loading screen. */
    void startLinux(long terminalFor) {
        if (!Cover.available(this)) {
            openTerminal();                       // no "display over other apps": the old way
            return;
        }
        Intent i = getPackageManager().getLaunchIntentForPackage(TERMINAL_PACKAGE);
        if (i == null) {
            web.evaluateJavascript("window.noTerminal && window.noTerminal()", null);
            return;
        }
        terminalOpenedAt = System.currentTimeMillis();
        ProxyService.startWaiting(this);          // safety net: the service brings us back too
        Cover.show(this);
        ui.postDelayed(() -> {
            try {
                startActivity(i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
            } catch (Exception e) {
                Cover.hide(this);
            }
        }, 200);
        ui.postDelayed(() -> {
            if (visible) return;
            try {
                startActivity(new Intent(this, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK
                        | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT | Intent.FLAG_ACTIVITY_SINGLE_TOP));
            } catch (Exception e) {
                Cover.hide(this);
            }
        }, 200 + terminalFor);
    }

    /** Linux didn't start: try again with the Terminal up longer; the last try shows the Terminal
     *  (in case it needs you) but still comes back here by itself after 20 s. */
    void retryLinux() {
        attempts++;
        if (attempts == 1) {
            startLinux(10000);
            return;
        }
        openTerminal();
        ui.postDelayed(() -> {
            if (visible) return;
            try {
                startActivity(new Intent(this, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK
                        | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT | Intent.FLAG_ACTIVITY_SINGLE_TOP));
            } catch (Exception ignored) {
            }
        }, 20000);
    }

    void openTerminal() {
        terminalOpenedAt = System.currentTimeMillis();
        Intent i = getPackageManager().getLaunchIntentForPackage(TERMINAL_PACKAGE);
        if (i == null) {
            web.evaluateJavascript("window.noTerminal && window.noTerminal()", null);
            return;
        }
        ProxyService.startWaiting(this);
        startActivity(i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
    }

    @Override
    protected void onResume() {
        super.onResume();
        visible = true;
        web.resumeTimers();
        web.onResume();
        Cover.hideSoon(this);
        ProxyService.cancelReadyNotice(this);
        if (showingStart) loadOrStart();
        else reconnect();          // Linux may have been stopped while you were away
    }

    @Override
    protected void onPause() {
        visible = false;
        // battery: stop the page's timers and checks while you're away (the background
        // helper still watches for questions and finished discussions)
        web.onPause();
        web.pauseTimers();
        super.onPause();
    }

    @Override
    protected void onActivityResult(int request, int result, Intent data) {
        if (request == 41 && fileCallback != null) {
            fileCallback.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(result, data));
            fileCallback = null;
            return;
        }
        super.onActivityResult(request, result, data);
    }

    @Override
    public void onBackPressed() {
        if (!showingStart && web.canGoBack()) web.goBack();
        else moveTaskToBack(true);      // keep running in the background, like other apps
    }

    /** What the Amalgam page can ask of the Android app. */
    class Bridge {
        @JavascriptInterface
        public void setToken(String token) {
            ProxyService.setToken(MainActivity.this, token);
        }

        @JavascriptInterface
        public String proxyInfo() {
            return "{\"port\":" + ProxyService.PORT + ",\"user\":\"amalgam\",\"password\":\""
                    + ProxyService.password(MainActivity.this) + "\"}";
        }

        @JavascriptInterface
        public String version() {
            return BuildConfigVersion.name(MainActivity.this);
        }

        @JavascriptInterface
        public void share(String title, String text) {
            Intent send = new Intent(Intent.ACTION_SEND).setType("text/plain")
                    .putExtra(Intent.EXTRA_SUBJECT, title).putExtra(Intent.EXTRA_TEXT, text);
            runOnUiThread(() -> startActivity(Intent.createChooser(send, title)));
        }

        @JavascriptInterface
        public void openTerminal() {
            runOnUiThread(MainActivity.this::openTerminal);
        }

        @JavascriptInterface
        public void reconnect() {
            runOnUiThread(MainActivity.this::reconnect);
        }

        @JavascriptInterface
        public void retryLinux() {
            runOnUiThread(MainActivity.this::retryLinux);
        }

        @JavascriptInterface
        public String startInfo() {
            return "tries: " + (attempts + 1) + " · " + ProxyService.prefs(MainActivity.this).getString("return_log", "");
        }

        @JavascriptInterface
        public long lastStart() {
            return ProxyService.prefs(MainActivity.this).getLong("last_start_ms", 0);
        }

        @JavascriptInterface
        public boolean quietStart() {
            return Cover.available(MainActivity.this);
        }

        @JavascriptInterface
        public boolean canAutoReturn() {
            return Settings.canDrawOverlays(MainActivity.this);
        }

        @JavascriptInterface
        public boolean askedAutoReturn() {
            return ProxyService.prefs(MainActivity.this).getBoolean("asked_auto_return", false);
        }

        @JavascriptInterface
        public void askAutoReturn() {
            ProxyService.prefs(MainActivity.this).edit().putBoolean("asked_auto_return", true).apply();
            Intent i = new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:" + getPackageName()));
            runOnUiThread(() -> {
                try {
                    startActivity(i);
                } catch (ActivityNotFoundException e) {
                    startActivity(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION));
                }
            });
        }

        @JavascriptInterface
        public void skipAutoReturn() {
            ProxyService.prefs(MainActivity.this).edit().putBoolean("asked_auto_return", true).apply();
            runOnUiThread(MainActivity.this::openTerminal);
        }

        @JavascriptInterface
        public void keepAliveSetup() {
            KeepAlive.start(MainActivity.this);
        }

        @JavascriptInterface
        public String keepAliveState() {
            return KeepAlive.done(MainActivity.this) ? "done" : KeepAlive.status;
        }

        @JavascriptInterface
        public String returnInfo() {
            return ProxyService.prefs(MainActivity.this).getString("return_log", "");
        }

        @JavascriptInterface
        public int stage() {
            return amalgamStage();
        }

        @JavascriptInterface
        public boolean isUp() {
            return amalgamUp();
        }

        @JavascriptInterface
        public void goHome() {
            runOnUiThread(MainActivity.this::goHome);
        }
    }
}
