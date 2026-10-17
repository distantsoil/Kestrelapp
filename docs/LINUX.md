# Kestrel on Linux

Kestrel for Linux ships as one file, `Kestrel-x.y.z-x86_64.AppImage`. Nothing is installed
system-wide and nothing needs administrator rights except, on some systems, the one library below.

## Install and open

1. Download `Kestrel-x.y.z-x86_64.AppImage` from the release page.
2. Make it executable. In most file managers: right-click the file, choose **Properties**, then
   **Permissions**, and tick **Allow executing file as program**. In a terminal it is one line:

   ```
   chmod +x Kestrel-*-x86_64.AppImage
   ```

3. Double-click it. The first start takes a little longer while Kestrel prepares its data folder.

## If it does not open: libfuse2

Like every AppImage, Kestrel needs the FUSE 2 library. Most desktops have it already; recent
Ubuntu releases (22.04 and later) and some minimal installs do not. Add it with one line:

| System | Command |
|---|---|
| Ubuntu 24.04 and later, Debian 13 | `sudo apt install libfuse2t64` |
| Ubuntu 22.04, Debian 12, Linux Mint, Pop!_OS | `sudo apt install libfuse2` |
| Fedora | `sudo dnf install fuse-libs` |
| Arch, Manjaro, EndeavourOS | `sudo pacman -S fuse2` |

No FUSE at all (a container, or a locked-down machine)? Run it without FUSE instead; the AppImage
unpacks itself into a temporary folder each time:

```
./Kestrel-*-x86_64.AppImage --appimage-extract-and-run
```

## The window, and why it may open in your browser

Kestrel draws its own window with GTK and WebKit2GTK. When those cannot be loaded, Kestrel does
not refuse to start: it opens in your default browser instead, and the first page says so in a
short panel with the details under **Show details**. Everything works the same in the browser.
Closing the tab does not stop Kestrel; open Kestrel again from your applications menu and the tab
comes back.

The AppImage does not carry the GTK bindings for Python, so on Linux it uses your browser as its
window. Running Kestrel from its source code (`python -m kestrel.desktop`) does use a window
when the system has WebKit2GTK and the Python running Kestrel can import PyGObject (for example a
virtual environment created with `--system-site-packages` after installing the packages below):

| System | Command |
|---|---|
| Debian, Ubuntu, Linux Mint, Pop!_OS | `sudo apt install gir1.2-webkit2-4.1 libwebkit2gtk-4.1-0 python3-gi` |
| Fedora | `sudo dnf install webkit2gtk4.1 python3-gobject` |
| Arch, Manjaro, EndeavourOS | `sudo pacman -S webkit2gtk-4.1 python-gobject` |

## The tray icon

The tray icon needs an X display, or on GNOME the AppIndicator extension
(`gnome-shell-extension-appindicator` on Debian, Ubuntu and Fedora). Without one, Kestrel runs
without a tray icon; the window or browser tab is all you need.

## Where Kestrel keeps your data

Your data folder is `~/.local/share/kestrel` (or under `$XDG_DATA_HOME` when that is set).
Deleting the AppImage does not delete it. The Backup page writes a zip of it at any time.
