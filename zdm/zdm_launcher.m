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
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifndef ZDM_APP_DIR
#define ZDM_APP_DIR ""
#endif

static void zdm_bail(const char *msg) {
    fprintf(stderr, "ZDM: %s\n", msg);
    fflush(stderr);
    /* Give the user something on screen if we die immediately at launch. */
    fprintf(stderr, "ZDM: fatal error during startup\n");
    exit(1);
}

int main(int argc, char **argv) {
    const char *app_dir = getenv("ZDM_APP_DIR");
    if (!app_dir || !*app_dir) app_dir = ZDM_APP_DIR;
    if (!app_dir || !*app_dir) {
        zdm_bail("ZDM_APP_DIR not set and no compiled-in fallback");
    }

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

    /* Start the backend service if it is not already listening. */
    PyRun_SimpleString(
        "import os,sys,subprocess,time,urllib.request\n"
        "app_dir = os.environ.get('ZDM_APP_DIR','')\n"
        "def _alive():\n"
        "    try:\n"
        "        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:9000/api/state', timeout=2):\n"
        "            return True\n"
        "    except Exception:\n"
        "        return False\n"
        "if app_dir and not _alive():\n"
        "    py = os.path.join(app_dir,'venv','bin','python')\n"
        "    sc = os.path.join(app_dir,'zdm.py')\n"
        "    try:\n"
        "        subprocess.Popen([py,sc,'serve'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)\n"
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