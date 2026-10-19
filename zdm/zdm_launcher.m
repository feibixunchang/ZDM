/*
 * ZDM native launcher (macOS).
 *
 * This is the real main executable of ZDM.app. It is a genuine Mach-O binary, so
 * LaunchServices / the Dock / Cocoa all treat the running process as ZDM.app:
 *   - the Dock icon is ZDM's own icon,
 *   - clicking the Dock icon delivers applicationShouldHandleReopen: to us, and
 *     PyQt5's QApplication (which is itself a native NSApplication on macOS)
 *     turns that into a Qt activation / reopen event that restores the window.
 *
 * The trick that makes this work: instead of `exec`-ing a separate python
 * process (which would lose the bundle identity because the new process image is
 * Python.app, not ZDM.app), we embed the CPython interpreter *inside this same
 * process* via the Python C API, then import and run ZDM's Qt client. There is
 * exactly one process, and it is ZDM.app.
 */

#include <Python.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* Compiled-in last-resort path. The build passes the staging dir, which is only
 * valid while the app sits where it was built, so this must never take priority
 * over runtime discovery -- otherwise a shipped .dmg would look for its Python
 * runtime inside the developer's build directory and fail to start. */
#ifndef ZDM_APP_DIR
#define ZDM_APP_DIR ""
#endif

/* True when <dir> looks like a usable ZDM app resource dir. */
static int zdm_dir_ok(const char *dir) {
    char probe[4096];
    if (!dir || !*dir) return 0;
    snprintf(probe, sizeof(probe), "%s/zdm.py", dir);
    return access(probe, R_OK) == 0;
}

/* Derive <exe>/../Resources from our own executable path. This is the only
 * source that stays correct after the .app is moved (dmg -> /Applications,
 * dragged to another folder, renamed, ...). */
static int zdm_app_dir_from_exe(char *out, size_t n) {
    char exe_path[4096];
    uint32_t exe_size = sizeof(exe_path);
    char real[4096];
    char *slash;

    out[0] = '\0';
    if (_NSGetExecutablePath(exe_path, &exe_size) != 0) return 0;
    if (!realpath(exe_path, real)) return 0;
    slash = strrchr(real, '/'); /* .../Contents/MacOS/ZDM -> .../Contents/MacOS */
    if (!slash) return 0;
    *slash = '\0';
    slash = strrchr(real, '/'); /* .../Contents/MacOS -> .../Contents */
    if (!slash) return 0;
    *slash = '\0';
    snprintf(out, n, "%s/Resources", real); /* keep the path free of ".." */
    return zdm_dir_ok(out);
}

static void zdm_bail(const char *msg) {
    fprintf(stderr, "ZDM: %s\n", msg);
    fflush(stderr);
    /* Give the user something on screen if we die immediately at launch. */
    fprintf(stderr, "ZDM: fatal error during startup\n");
    exit(1);
}

int main(int argc, char **argv) {
    /* Resolution order, most reliable first:
     *   1. <own executable>/../Resources  -- correct wherever the app is moved
     *   2. $ZDM_APP_DIR                   -- set by installers / wrappers
     *   3. compiled-in path               -- only valid in the build tree
     * Each candidate is validated, so a stale one is skipped instead of
     * silently pointing Python at a directory that no longer exists. */
    char resolved_dir[4096];
    const char *app_dir = NULL;

    if (zdm_app_dir_from_exe(resolved_dir, sizeof(resolved_dir))) {
        app_dir = resolved_dir;
    }
    if (!app_dir) {
        const char *env_dir = getenv("ZDM_APP_DIR");
        if (zdm_dir_ok(env_dir)) app_dir = env_dir;
    }
    if (!app_dir && zdm_dir_ok(ZDM_APP_DIR)) {
        app_dir = ZDM_APP_DIR;
    }
    if (!app_dir) {
        zdm_bail("cannot locate the app resource directory");
    }

    /* Debug hook used by the build's relocation test: report the discovered
     * resource dir and exit before touching Python/Qt. */
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--zdm-print-app-dir") == 0) {
            printf("%s\n", app_dir);
            fflush(stdout);
            return 0;
        }
    }

    /* Export it: the bootstrap Python below reads ZDM_APP_DIR to locate zdm.py
     * and the bundled venv. Without this the service would never be started
     * (getenv() returned NULL and the inline script silently did nothing). */
    setenv("ZDM_APP_DIR", app_dir, 1);

    /* Make sure a stale ZDM desktop instance does not fight this one for the
     * single-instance local socket. gui.py handles the real single-instance
     * logic; we just make sure PYTHONPATH and argv[0] are sane. */

    /* Prepend the app's site-packages (the venv) so `import PyQt5` works. */
    const char *sp = getenv("ZDM_SITE_PACKAGES");
    if (sp && *sp) {
        /* Respect an existing PYTHONPATH if the installer set one. */
    }

    /* Initialise the embedded interpreter. */
    PyConfig config;
    PyConfig_InitIsolatedConfig(&config);
    /* Keep the process name as the bundle executable for NSBundle lookups. */
    PyConfig_SetBytesArgv(&config, argc, argv);

    /* Force UTF-8 for stdio. Without this, stdout/stderr default to the ASCII
     * codec when the app is launched from Finder/CI (no TTY), and any Chinese
     * text printed by gui.py raises UnicodeEncodeError and aborts startup. */
    PyConfig_SetBytesString(&config, &config.stdio_encoding, "utf-8");
    /* Also make the filesystem encoding UTF-8 so paths with non-ASCII chars work. */
    PyConfig_SetBytesString(&config, &config.filesystem_encoding, "utf-8");

    /* Program name influences sys.executable / prefix discovery. Point it at the
     * venv interpreter that owns PyQt5 so the venv's site-packages is found. */
    char venv_python[4096];
    snprintf(venv_python, sizeof(venv_python), "%s/venv/bin/python", app_dir);
    if (access(venv_python, X_OK) == 0) {
        PyConfig_SetBytesString(&config, &config.program_name, venv_python);
    }

    PyStatus status = Py_InitializeFromConfig(&config);
    PyConfig_Clear(&config);
    if (PyStatus_Exception(status)) {
        zdm_bail("failed to initialise the embedded Python interpreter");
    }

    /* sys.path: venv site-packages + the app dir (for `import gui`, `import zdm`). */
    char spath[4096];
    snprintf(spath, sizeof(spath), "%s/venv/lib/python3.9/site-packages", app_dir);
    PyObject *sys_path = PySys_GetObject("path");
    if (sys_path) {
        PyObject *p1 = PyUnicode_FromString(spath);
        PyObject *p2 = PyUnicode_FromString(app_dir);
        if (p1 && p2) {
            PyList_Insert(sys_path, 0, p2);
            PyList_Insert(sys_path, 0, p1);
        }
        Py_XDECREF(p1);
        Py_XDECREF(p2);
    }

    /* Start the backend service if it is not already listening.
     *
     * NOTE: the port comes from the user's config (default 9000) so an app on a
     * custom port still finds/starts the right service. */
    PyRun_SimpleString(
        "import os,sys,subprocess,time,urllib.request\n"
        "app_dir = os.environ.get('ZDM_APP_DIR','')\n"
        "try:\n"
        "    from zdm.manager import load_config\n"
        "    port = int(load_config().get('port') or 9000)\n"
        "except Exception:\n"
        "    port = 9000\n"
        "def _alive():\n"
        "    try:\n"
        "        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))\n"
        "        opener.open('http://127.0.0.1:%d/api/state' % port, timeout=2)\n"
        "        return True\n"
        "    except Exception:\n"
        "        return False\n"
        "if app_dir and not _alive():\n"
        "    py = os.path.join(app_dir,'venv','bin','python')\n"
        "    if not os.path.exists(py): py = sys.executable\n"
        "    sc = os.path.join(app_dir,'zdm.py')\n"
        "    try:\n"
        "        subprocess.Popen([py,sc,'serve','--port',str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)\n"
        "    except Exception:\n"
        "        pass\n"
        "    for _ in range(30):\n"
        "        if _alive(): break\n"
        "        time.sleep(0.3)\n"
    );

    /* Hand over to the Qt desktop client. gui.main() builds the QApplication
     * (our ZDMApplication subclass) and enters its event loop. On macOS Qt runs
     * the native Cocoa run loop inside this very process, so the Dock icon and
     * the Dock-click reopen callback are handled natively.
     *
     * We pass argv=None so gui.main() reads sys.argv directly. We already set
     * sys.argv via PyConfig_SetBytesArgv, so it is ["ZDM", "9000"] and
     * gui.main() picks up the port from sys.argv[1]. */
    PyObject *gui = PyImport_ImportModule("gui");
    if (!gui) {
        PyErr_Print();
        zdm_bail("could not import the gui module");
    }

    PyObject *main_fn = PyObject_GetAttrString(gui, "main");
    if (!main_fn) {
        PyErr_Print();
        zdm_bail("gui module has no main()");
    }

    /* Call gui.main(None) -> uses sys.argv. */
    PyObject *result = PyObject_CallFunction(main_fn, "O", Py_None);

    int rc = 0;
    if (!result) {
        PyErr_Print();
        rc = 1;
    } else {
        PyObject *rc_obj = PyNumber_Long(result);
        if (rc_obj) {
            rc = (int)PyLong_AsLong(rc_obj);
            Py_DECREF(rc_obj);
        }
        Py_DECREF(result);
    }

    Py_XDECREF(main_fn);
    Py_XDECREF(gui);
    Py_Finalize();
    return rc;
}