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

    static boolean amalgamUp() {
        try (Socket sock = new Socket()) {
            sock.connect(new InetSocketAddress("127.0.0.1", 8765), 700);
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    private void loadOrStart() {
        new Thread(() -> {
            boolean up = amalgamUp();
            runOnUiThread(() -> {
                if (up) goHome();
                else showStart(true);
            });
        }).start();
    }

    private void goHome() {
        showingStart = false;
        web.loadUrl(HOME);
    }

    /** "Starting AI Amalgam" screen; opens the Terminal app (which starts it) once. */
    private void showStart(boolean openTerminalNow) {
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
            new Handler(Looper.getMainLooper()).postDelayed(this::openTerminal, 600);
        }
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
        ProxyService.cancelReadyNotice(this);
        if (showingStart) loadOrStart();
    }

    @Override
    protected void onPause() {
        visible = false;
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
        public boolean isUp() {
            return amalgamUp();
        }

        @JavascriptInterface
        public void goHome() {
            runOnUiThread(MainActivity.this::goHome);
        }
    }
}
