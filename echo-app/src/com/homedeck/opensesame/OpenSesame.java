package com.homedeck.opensesame;

import android.accessibilityservice.AccessibilityService;
import android.content.Intent;
import android.os.SystemClock;
import android.util.Log;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import java.util.HashSet;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Watches Alexa's conversation overlay. Each thing you say (or type) appears there in its own numbered
 * group ("user-node-fragment-group-2.0"). When the newest group reads "open sesame", the deck opens.
 * Older groups that reappear in the history are ignored: only the highest-numbered one counts, whatever
 * is showing when the overlay opens counts as old, and each group fires at most once.
 */
public class OpenSesame extends AccessibilityService {
    private static final String TAG = "OpenSesame";
    private static final String DECK = "de.ozerov.fully";
    private static final Pattern PHRASE = Pattern.compile("^\\W*open[\\s,]+sesame\\W*$", Pattern.CASE_INSENSITIVE);
    private static final Pattern GROUP = Pattern.compile("user-node-fragment-group-([\\d.]+)");

    private final Set<String> done = new HashSet<>();  // groups already handled while the overlay is open
    private boolean drawerOpen;
    private long closeUntil, lastClose;  // Alexa's reply reopens the overlay, so keep closing it for a while

    // what one scan of the overlay found
    private String topId, topText;
    private double topIndex;
    private boolean drawer;
    private AccessibilityNodeInfo dismiss;  // the overlay's own "Dismiss" button

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        topId = null; topText = null; topIndex = -1; drawer = false; dismiss = null;
        // the Echo's window list leaves out app windows, so climb from the event to the top of its window
        AccessibilityNodeInfo root = event.getSource();
        while (root != null && root.getParent() != null) root = root.getParent();
        if (root == null) root = getRootInActiveWindow();
        if (root == null) return;
        scan(root, 0);
        if (Log.isLoggable(TAG, Log.DEBUG)) Log.d(TAG, "drawer=" + drawer + " top=" + topId + " text=" + topText);
        long now = SystemClock.uptimeMillis();
        if (drawer && now < closeUntil && dismiss != null && now - lastClose > 300) {
            lastClose = now;
            dismiss.performAction(AccessibilityNodeInfo.ACTION_CLICK);
            return;
        }
        if (!drawer) { drawerOpen = false; done.clear(); return; }
        if (!drawerOpen) {  // the overlay just opened: whatever is already showing is old
            drawerOpen = true;
            if (topId != null) done.add(topId);
            return;
        }
        if (topId != null && !done.contains(topId) && topText != null && PHRASE.matcher(topText).matches()) {
            done.add(topId);
            Log.i(TAG, "heard open sesame, opening the deck");
            closeUntil = now + 12000;  // close Alexa's overlay now and whenever its reply brings it back
            lastClose = now;
            if (dismiss != null) dismiss.performAction(AccessibilityNodeInfo.ACTION_CLICK);
            Intent open = getPackageManager().getLaunchIntentForPackage(DECK);
            if (open != null) {
                open.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT);
                startActivity(open);
            }
        }
    }

    private void scan(AccessibilityNodeInfo node, int depth) {
        if (node == null || depth > 40) return;
        if (node.getContentDescription() != null && "Dismiss".contentEquals(node.getContentDescription())) dismiss = node;
        String id = node.getViewIdResourceName();
        if (id != null) {
            if (id.endsWith("conversationDrawer")) drawer = true;
            Matcher m = GROUP.matcher(id);
            if (m.matches()) {
                double index;
                try { index = Double.parseDouble(m.group(1)); } catch (NumberFormatException e) { index = 0; }
                String text = textIn(node, 0);  // empty placeholder groups come and go while Alexa listens: skip them
                if (text != null && index >= topIndex) { topIndex = index; topId = id; topText = text; }
                return;
            }
        }
        for (int i = 0; i < node.getChildCount(); i++) scan(node.getChild(i), depth + 1);
    }

    private static String textIn(AccessibilityNodeInfo node, int depth) {
        if (node == null || depth > 10) return null;
        if (node.getText() != null && node.getText().length() > 0) return node.getText().toString();
        for (int i = 0; i < node.getChildCount(); i++) {
            String t = textIn(node.getChild(i), depth + 1);
            if (t != null) return t;
        }
        return null;
    }

    @Override
    public void onInterrupt() {}
}
