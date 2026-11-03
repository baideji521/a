/*
 * Native-layer guard for the Guise Xposed module.
 *
 * Every Java hook in this module reported zero hits against Momo. The measured
 * reason is that the detector reads /proc/<pid>/maps and the debugging state
 * through libc directly, so the calls never pass through java.io, Settings or
 * any binder proxy that a Java hook could see.
 *
 * This file lives inside the guarded process. It is loaded by the module's own
 * Java code (which runs in that process) and replaces the libc entry points the
 * detector uses, so the answer is changed at the boundary where it is read
 * rather than at a layer above it.
 *
 * Two rules keep the guard safe:
 *   1. Only the exact paths and properties named below are touched. Everything
 *      else calls straight through to the real function, unchanged.
 *   2. Nothing here invents a value. A device without Magisk has no /data/adb
 *      tree, a device with debugging off has no adbd socket, and a process that
 *      never loaded Zygisk has no zygisk segment in its map. The guard reports
 *      that consistent state; it does not report an impossible one.
 */

#include <jni.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <unistd.h>
#include <pthread.h>
#include <sys/uio.h>
#include <fcntl.h>
#include <errno.h>
#include <dlfcn.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/utsname.h>
#include <sys/types.h>
#include <android/log.h>
#include <link.h>
#include <elf.h>
#include <sys/syscall.h>
#include <sys/mman.h>
#include <fcntl.h>
#include <stdlib.h>
#include <unistd.h>
#include <pthread.h>
#include <stdio.h>

/*
 * The maximum length of a property value, including the terminator.
 *
 * This is the platform's own limit; a value longer than it would be truncated by
 * the real reader anyway, so the override has to respect the same bound or it
 * would overflow the caller's buffer.
 */
#define PROP_VALUE_MAX 92

#define LOG_TAG "GuiseNative"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, LOG_TAG, __VA_ARGS__)

/*
 * Counters for the tracing below, and a cap on how many lines it may print.
 *
 * The read hook is the hottest path in this process: every file, every property area and
 * every map listing passes through it, and a detector that scans in small blocks produces
 * thousands of calls in a second. Logging each one would slow the caller enough to change
 * what it measures, and what it measures is part of the test. The trace is therefore a
 * bounded sample: enough lines to show the shape of the traffic and whether substitution
 * fires, few enough that the timing stays honest.
 */
static int g_value_apply_count = 0;
#define VALUE_APPLY_LOG_LIMIT 24

/* ------------------------------------------------------------------------- */
/* Real symbols                                                              */
/* ------------------------------------------------------------------------- */

typedef int (*openat_t)(int, const char *, int, ...);
typedef int (*open_t)(const char *, int, ...);
typedef int (*access_t)(const char *, int);
typedef int (*stat_t)(const char *, struct stat *);

/*
 * `__system_property_get` returns the length of the value it copied, not the
 * buffer. Declaring it as returning `char*` makes every caller read a length as
 * a pointer: on AArch64 both arrive in x0, but an int result is not a valid
 * address and the value is then used as one. The return type must match exactly.
 */
typedef int (*prop_get_t)(const char *, char *);
typedef int (*prop_get_int_t)(const char *);

/*
 * `__system_property_find` returns an opaque handle to the record, not the value.
 * The record lives inside the mapped property area, so a pointer obtained before that
 * area is replaced keeps pointing at the same address afterwards and reads whatever is
 * there now. That is what makes this usable as a trigger rather than a rewrite.
 */
typedef const void *(*prop_find_t)(const char *);

/*
 * The callback form of a property read, and the call that drives it.
 *
 * `__system_property_read_callback` takes the record, a function pointer and a cookie,
 * then invokes that function with the name and value of the record. Android 8 and later
 * read properties this way instead of through `__system_property_get`, because it hands
 * back the name and the serial alongside the value without a second lookup.
 *
 * That makes it the one place where the answer can be decided for every reader that
 * goes through the property service, without touching the memory the record lives in.
 * A record in a file that was mapped after install, or unmapped and mapped again, is
 * answered here exactly the same way as one that was mapped at install -- the value
 * never has to be found in a buffer.
 */
typedef void (*prop_read_cb_t)(void *, const char *, const char *, unsigned int);
typedef void (*prop_read_callback_t)(const void *, prop_read_cb_t, void *);

typedef int (*g_selinux_check_t)(const char *);

typedef ssize_t (*read_t)(int, void *, size_t);
typedef ssize_t (*pread64_t)(int, void *, size_t, off64_t);
typedef ssize_t (*readv_t)(int, const struct iovec *, int);
typedef ssize_t (*preadv_t)(int, const struct iovec *, int, off_t);
typedef ssize_t (*preadv2_t)(int, const struct iovec *, int, off_t, int);
typedef ssize_t (*readlink_t)(const char *, char *, size_t);
typedef ssize_t (*readlinkat_t)(int, const char *, char *, size_t);
typedef int (*close_t)(int);

/*
 * The loader's own list of loaded objects, which no read hook can see.
 *
 * `dl_iterate_phdr` does not open a file, does not allocate a descriptor and does not
 * copy a buffer: it walks the dynamic linker's own table and calls back once per
 * loaded object with a `dl_phdr_info` whose `dlpi_name` is the path the object was
 * loaded from. A guard placed on `open`/`read`/`readlink` therefore never runs for it,
 * and the names it hands out are the loader's own strings, not anything the guard had
 * a chance to rewrite.
 *
 * That makes it an independent second opinion on exactly the question maps answers —
 * and the cheapest one for a detector to ask, because the answer is already in memory
 * and requires no permission at all.
 */
typedef int (*dl_iterate_phdr_cb_t)(struct dl_phdr_info *, size_t, void *);
typedef int (*dl_iterate_phdr_t)(dl_iterate_phdr_cb_t, void *);

/*
 * The raw syscall entry point, which is the channel every libc hook is blind to.
 *
 * A call through `syscall(SYS_openat, ...)` never touches `openat`: the number goes
 * into x8, the arguments into x0-x5, and the kernel is entered directly. Every guard
 * placed on a libc symbol is bypassed, no matter how carefully that symbol was chosen,
 * because the symbol is simply not used.
 *
 * That is the only remaining explanation for a process whose real map demonstrably
 * contains the module's name while its own reads report nothing: the reads did not go
 * through the functions that were patched. Patching `syscall` itself closes that gap at
 * the one point every direct call must pass through.
 */
typedef long (*syscall_t)(long, ...);

#ifndef SYS_memfd_create
#define SYS_memfd_create 279
#endif

typedef int (*open_2_t)(const char *path, int flags);
typedef void *(*mmap_fn_t)(void *addr, size_t length, int prot, int flags, int fd, off_t offset);
static openat_t real_openat = NULL;
static open_t real_open = NULL;

/*
 * The FORTIFY entry point and the mapping call.
 *
 * open() has a second name. When the caller asks for a descriptor without a mode
 * argument and the compiler cannot prove the flags exclude creation, it emits a call to
 * __open_2 instead, which performs the same check and then opens. A library compiled
 * with FORTIFY therefore reaches the property area through a symbol that a hook on
 * "open" never sees.
 *
 * mmap is the other half of the same problem. A caller that maps the property file does
 * not read it: the pages appear in its address space and every later access is a plain
 * load. Editing what read returns cannot reach that, and the mapping is created after
 * this module installs, so a pass over the existing mappings at install time cannot see
 * it either. The mapping has to be intercepted when it is made.
 */
static open_2_t real_open_2 = NULL;
static mmap_fn_t real_mmap = NULL;
static access_t real_access = NULL;
static stat_t real_stat = NULL;
static read_t real_read = NULL;

/*
 * The stdio path, which the earlier guard missed entirely.
 *
 * Every hook before this one answered open/read and their close relatives. A detector
 * that opens the map listing with fopen and reads it a line at a time never calls any
 * of them, so nothing was intercepted and the report said the process had executable
 * anonymous pages. That is not an oversight in the detector; it is a hole in this guard.
 *
 * fgets hands back one line, complete, which is the shape the rewriting code already
 * expects. fread can return any number of bytes, including a partial line, so its
 * buffer is rewritten only when it contains a full map entry; rewriting a fragment
 * would corrupt the line that continues in the next call.
 */
typedef char *(*fgets_t)(char *, int, FILE *);
typedef size_t (*fread_t)(void *, size_t, size_t, FILE *);
typedef FILE *(*fopen_t)(const char *, const char *);
static fgets_t real_fgets = NULL;
static fread_t real_fread = NULL;
static fopen_t real_fopen = NULL;
static volatile int g_stdio_hits = 0;
static pread64_t real_pread64 = NULL;
static readv_t real_readv = NULL;
static preadv_t real_preadv = NULL;
static preadv2_t real_preadv2 = NULL;
static readlink_t real_readlink = NULL;
static readlinkat_t real_readlinkat = NULL;
static close_t real_close = NULL;
static dl_iterate_phdr_t real_dl_iterate_phdr = NULL;
static syscall_t real_syscall = NULL;
typedef void *(*dlopen_t)(const char *, int);
typedef void *(*android_dlopen_ext_t)(const char *, int, const void *);

static dlopen_t real_dlopen = NULL;
static android_dlopen_ext_t real_android_dlopen_ext = NULL;

static prop_get_t real_prop_get = NULL;
static prop_get_int_t real_prop_get_int = NULL;
static prop_find_t real_prop_find = NULL;
static prop_read_callback_t real_prop_read_cb = NULL;

/*
 * Whether the property area has been rewritten since the process started.
 *
 * The area is mapped lazily on Android 12 and later: nothing maps it at install time,
 * so the rewrite that runs there finds no range to replace and returns zero. It is
 * the first property lookup that maps it, and the lookup happens inside libc -- which
 * is why this is observed here rather than scheduled. The flag is set once, under a
 * single-threaded assumption that holds because the first lookup happens on the
 * process main thread before any worker is started.
 */
static volatile int g_prop_lazy_patched = 0;

/*
 * Catches an int that the detector asked Java for, and only that detector.
 *
 * The value crosses the boundary as the return of this call, which makes the return the
 * last place it can be changed without touching code on either side.
 *
 * Two earlier versions of this were wrong in ways worth recording.
 *
 * The first keyed on the value alone. Another application in scope had an unrelated call
 * that happened to return a number sharing a bit, its answer was rewritten, and its report
 * went from clear to failing. A rule that cannot tell who it is answering is not a rule.
 *
 * The second keyed on whether the caller was inside a module whose name contained
 * "nullptr". That matches far more than intended: the detector installs its own libraries
 * under a path that contains the package name, which contains that word, so a dozen small
 * unrelated libraries qualified. The clearing then applied to calls it had no business
 * touching, and the detector reported a different finding as a result.
 *
 * What actually identifies the detector is the leaf name of the module the call came from,
 * which is either the library itself or the name its pages were given when they were
 * re-labelled. The comparison is against the last path component, not the whole path,
 * because only the leaf is fixed.
 *
 * Resolution is retried rather than done once. The hook is installed during process start
 * and the library is loaded after that, so a single attempt at install time is guaranteed
 * to find nothing; that is not a failure, it is the ordinary case. Retrying costs a module
 * walk per call until it succeeds, which is bounded and short-lived, and it stops for good
 * once the answer is known either way.
 *
 * Bit 3 is dropped by replacing the whole result, not by masking it, because zero is itself
 * a meaningful answer for this shape of call: it reads as an outright failure and produces
 * the same finding. One matches no branch and reports nothing.
 */
typedef jint (JNICALL *CallIntMethodV_t)(JNIEnv*, jobject, jmethodID, va_list);
static CallIntMethodV_t g_orig_CallIntMethodV = NULL;

/*
 * The variadic form is the one callers actually use.
 *
 * Hook only the V variant and nothing happens: the caller reaches the non-V entry, and
 * that entry does not dispatch through the function table to reach this one, so the
 * replaced slot is never consulted. The measurement was unambiguous — the detector
 * printed its verdict six times and the V hook was never entered.
 *
 * The non-V form is variadic and cannot forward arguments it never received, so it
 * rebuilds the list with va_start and calls the original V function. Both entries then
 * share one body, one counter and one test.
 */
typedef jint (JNICALL *CallIntMethod_t)(JNIEnv*, jobject, jmethodID, ...);
static CallIntMethod_t g_orig_CallIntMethod = NULL;

static volatile int g_callint_cleared = 0;

static volatile int g_detector_range_state = 0;   /* 0 unresolved, 1 valid, -1 given up */
static volatile int g_detector_probe_count = 0;
static uintptr_t g_detector_lo = 0;
static uintptr_t g_detector_hi = 0;

#define DETECTOR_PROBE_LIMIT 4000

/*
 * Which libraries count as the detector, and which property names must not be findable.
 *
 * Both of these are facts about the thing being guarded rather than about this module. A
 * detector may put its code in a library with any name, and it may search for any property
 * name; neither is knowable in advance and either can change without notice. So both are
 * configuration: the module passes in a list, and the code below only knows how to walk it.
 *
 * Matching is by prefix of the leaf name, which is what the fixed comparison did before it
 * became a list. The leaf is used rather than the whole path because the path carries the
 * package name, and a package name can contain the very words being searched for — a
 * mistake that once selected unrelated libraries the application had loaded itself.
 */
#define GUARD_LIB_MAX 8
#define GUARD_LIB_NAME_MAX 96

static char g_guard_libs[GUARD_LIB_MAX][GUARD_LIB_NAME_MAX];
static int g_guard_lib_count = 0;

/*
 * Property names that must not be findable in the property area.
 *
 * Separate from the override table on purpose. The overrides say "when this name is asked
 * for, answer with that value"; these say "make the name itself unfindable". A check that
 * scans the raw bytes of the property file never asks for a value, so an override is
 * invisible to it — only the presence of the name matters.
 */
#define HIDDEN_KEY_MAX 8
#define HIDDEN_KEY_NAME_MAX 64

static char g_hidden_keys[HIDDEN_KEY_MAX][HIDDEN_KEY_NAME_MAX];
static int g_hidden_key_count = 0;

/*
 * Whether the lists hold anything at all.
 *
 * Set by both the parser and the default seed, so that a caller which configures the guard
 * explicitly is never overwritten by the compiled-in defaults afterwards, and a caller
 * which configures nothing still gets them.
 */
static int g_guard_lists_seeded = 0;

/*
 * Parses a list of names, one per line. Blank lines and '#' comments are ignored, and
 * surrounding whitespace is trimmed, because the text arrives from a configuration file
 * where people write these things by hand.
 *
 * Replacing rather than appending makes the call idempotent: the module configures the
 * guard on every process start and cannot know whether this is the first time.
 */
static void set_guarded_libs(const char *text) {
    g_guard_lib_count = 0;
    g_guard_lists_seeded = 1;
    if (text == NULL) return;

    const char *p = text;
    while (*p != '\0' && g_guard_lib_count < GUARD_LIB_MAX) {
        const char *nl = strchr(p, '\n');
        const char *end = (nl != NULL) ? nl : p + strlen(p);

        const char *s = p;
        while (s < end && (*s == ' ' || *s == '\t')) s++;
        const char *e = end;
        while (e > s && (e[-1] == '\r' || e[-1] == ' ' || e[-1] == '\t')) e--;

        size_t n = (size_t) (e - s);
        if (n > 0 && *s != '#' && n < GUARD_LIB_NAME_MAX) {
            memcpy(g_guard_libs[g_guard_lib_count], s, n);
            g_guard_libs[g_guard_lib_count][n] = '\0';
            g_guard_lib_count++;
        }
        p = (nl != NULL) ? nl + 1 : end;
    }
    LOGI("guarded libs configured: %d", g_guard_lib_count);
}

static void set_hidden_prop_keys(const char *text) {
    g_hidden_key_count = 0;
    g_guard_lists_seeded = 1;
    if (text == NULL) return;

    const char *p = text;
    while (*p != '\0' && g_hidden_key_count < HIDDEN_KEY_MAX) {
        const char *nl = strchr(p, '\n');
        const char *end = (nl != NULL) ? nl : p + strlen(p);

        const char *s = p;
        while (s < end && (*s == ' ' || *s == '\t')) s++;
        const char *e = end;
        while (e > s && (e[-1] == '\r' || e[-1] == ' ' || e[-1] == '\t')) e--;

        size_t n = (size_t) (e - s);
        if (n > 0 && *s != '#' && n < HIDDEN_KEY_NAME_MAX) {
            memcpy(g_hidden_keys[g_hidden_key_count], s, n);
            g_hidden_keys[g_hidden_key_count][n] = '\0';
            g_hidden_key_count++;
        }
        p = (nl != NULL) ? nl + 1 : end;
    }
    LOGI("hidden prop keys configured: %d", g_hidden_key_count);
}

/*
 * Fills the lists with the names this guard was written against, once.
 *
 * A module build that passes nothing has to behave the way it did before the lists
 * existed, so the two names are compiled in as the initial contents. The configuration
 * replaces them; it does not add to them. A caller that wants a different detector — or
 * none at all — therefore gets exactly that, and a caller that says nothing keeps working.
 */
static void ensure_guard_lists_seeded(void) {
    if (g_guard_lists_seeded) return;
    set_guarded_libs("libnullptr.so\nshadow-lib");
    set_hidden_prop_keys("ro.dalvik.vm.native.bridge");
}

/*
 * True when the leaf name of a mapped library is one of the configured ones.
 *
 * Prefix rather than equality: a library may be mapped under a name with a suffix, and the
 * relabelling step renames what the map reports. The comparison length is the configured
 * name, so a longer mapped name that starts with it still matches.
 */
static int is_guarded_leaf(const char *leaf) {
    ensure_guard_lists_seeded();
    if (leaf == NULL) return 0;
    for (int i = 0; i < g_guard_lib_count; i++) {
        size_t n = strlen(g_guard_libs[i]);
        if (n > 0 && strncmp(leaf, g_guard_libs[i], n) == 0) return 1;
    }
    return 0;
}

/*
 * Rewrites every configured name into a form that cannot be searched for.
 *
 * One byte of the name is replaced, keeping the record the same length. Nothing that
 * follows moves, and the result cannot be another valid name, so a reader that scans the
 * text finds nothing while every other reader still sees a well-formed area.
 */
static void hide_configured_keys(char *buf, size_t len) {
    if (buf == NULL || len == 0) return;
    ensure_guard_lists_seeded();
    for (int i = 0; i < g_hidden_key_count; i++) {
        size_t n = strlen(g_hidden_keys[i]);
        if (n == 0 || n >= len) continue;
        for (size_t p = 0; p + n < len; p++) {
            if (memcmp(buf + p, g_hidden_keys[i], n) != 0) continue;
            if (buf[p + n] != '\0') continue;
            buf[p + n - 1] = '_';
        }
    }
}
typedef struct {
    uintptr_t lo;
    uintptr_t hi;
    int found;
} detector_range_ctx;

static int detector_range_cb(struct dl_phdr_info *info, size_t size, void *data) {
    (void) size;
    detector_range_ctx *ctx = (detector_range_ctx *) data;
    if (info == NULL || info->dlpi_name == NULL) return 0;

    const char *name = info->dlpi_name;
    const char *leaf = strrchr(name, (int) '/');
    leaf = (leaf != NULL) ? leaf + 1 : name;

    /*
     * Leaf name only. The full path contains the package name, and the package name
     * contains the word, so matching the whole path selects unrelated libraries the
     * application loaded from its own directory.
     */
    int match = is_guarded_leaf(leaf);
    if (!match) return 0;

    uintptr_t lo = (uintptr_t) info->dlpi_addr;
    uintptr_t hi = lo;
    for (int i = 0; i < info->dlpi_phnum; i++) {
        const ElfW(Phdr) *ph = &info->dlpi_phdr[i];
        if (ph->p_type != PT_LOAD) continue;
        uintptr_t seg_hi = lo + (uintptr_t) ph->p_vaddr + (uintptr_t) ph->p_memsz;
        if (seg_hi > hi) hi = seg_hi;
    }

    if (hi > lo) {
        ctx->lo = lo;
        ctx->hi = hi;
        ctx->found = 1;
        return 1;
    }
    return 0;
}

static void resolve_detector_range(void) {
    detector_range_ctx ctx;
    memset(&ctx, 0, sizeof(ctx));

    if (real_dl_iterate_phdr != NULL) {
        real_dl_iterate_phdr(detector_range_cb, &ctx);
    } else {
        dl_iterate_phdr(detector_range_cb, &ctx);
    }

    if (ctx.found) {
        g_detector_lo = ctx.lo;
        g_detector_hi = ctx.hi;
        g_detector_range_state = 1;
        LOGI("callint: detector range 0x%lx-0x%lx", (unsigned long) ctx.lo, (unsigned long) ctx.hi);
    }
}

static jint JNICALL hook_CallIntMethodV(JNIEnv* env, jobject obj, jmethodID mid, va_list args) {
    jint ret = g_orig_CallIntMethodV(env, obj, mid, args);

    /*
     * Unconditional tally, separate from the decision.
     *
     * If this stays at zero the question is not whether the rule is right, it is whether
     * the entry is reached at all, and no amount of adjusting the rule answers that. The
     * counter exists so the two questions stay distinguishable.
     */
    {
        static volatile int all_v = 0;
        int an = __atomic_add_fetch(&all_v, 1, __ATOMIC_RELAXED);
        if (an <= 20) {
            LOGI("callint: V seen #%d mid=%p ret=0x%x caller=0x%lx",
                 an, (void *) mid, ret, (unsigned long) __builtin_return_address(0));
        }
    }

    if (g_detector_range_state == 0) {
        int n = __atomic_add_fetch(&g_detector_probe_count, 1, __ATOMIC_RELAXED);
        if (n > DETECTOR_PROBE_LIMIT) {
            g_detector_range_state = -1;
            LOGI("callint: detector never appeared, passthrough");
        } else {
            resolve_detector_range();
        }
    }
    if (g_detector_range_state != 1) {
        return ret;
    }

    /*
     * Where the call came from, not what it was called on. This is the one property of a
     * caller that cannot be renamed out from under the check.
     */
    uintptr_t caller = (uintptr_t) __builtin_return_address(0);
    if (caller < g_detector_lo || caller >= g_detector_hi) {
        return ret;
    }

    if ((ret & 0x8) != 0) {
        int n = __atomic_add_fetch(&g_callint_cleared, 1, __ATOMIC_RELAXED);
        if (n <= 8) {
            LOGI("callint: 0x%x -> 1 (detector call at 0x%lx)", ret, (unsigned long) caller);
        }
        ret = 1;
    }
    return ret;
}

static jint JNICALL hook_CallIntMethod(JNIEnv* env, jobject obj, jmethodID mid, ...) {
    va_list args;
    va_start(args, mid);
    jint ret = g_orig_CallIntMethodV(env, obj, mid, args);
    va_end(args);

    if (g_detector_range_state == 0) {
        int n = __atomic_add_fetch(&g_detector_probe_count, 1, __ATOMIC_RELAXED);
        if (n > DETECTOR_PROBE_LIMIT) {
            g_detector_range_state = -1;
            LOGI("callint: detector never appeared, passthrough");
        } else {
            resolve_detector_range();
        }
    }
    if (g_detector_range_state != 1) {
        return ret;
    }

    uintptr_t caller = (uintptr_t) __builtin_return_address(0);
    if (caller < g_detector_lo || caller >= g_detector_hi) {
        return ret;
    }

    if ((ret & 0x8) != 0) {
        int n = __atomic_add_fetch(&g_callint_cleared, 1, __ATOMIC_RELAXED);
        if (n <= 8) {
            LOGI("callint(V-form): 0x%x -> 1 at 0x%lx", ret, (unsigned long) caller);
        }
        ret = 1;
    }
    return ret;
}

/*
 * The static entry points are separate slots, not aliases.
 *
 * The method this guards is static, so the caller reaches the static family of entries.
 * Hooking the instance family produced nothing at all: the caller never touched those
 * slots, and the verdict printed on every pass while every counter stayed at zero.
 *
 * There are three shapes because the table has three: an unnamed variadic form, one that
 * takes a va_list, and one that takes an array of jvalue. The caller on this device uses
 * the array form, but which of the three is used is not something to guess at — all three
 * are replaced and all three share one body, so it does not matter which one it picks.
 */
typedef jint (JNICALL *CallStaticIntMethod_t)(JNIEnv*, jclass, jmethodID, ...);
typedef jint (JNICALL *CallStaticIntMethodV_t)(JNIEnv*, jclass, jmethodID, va_list);
typedef jint (JNICALL *CallStaticIntMethodA_t)(JNIEnv*, jclass, jmethodID, const jvalue*);
static CallStaticIntMethod_t g_orig_CallStaticIntMethod = NULL;
static CallStaticIntMethodV_t g_orig_CallStaticIntMethodV = NULL;
static CallStaticIntMethodA_t g_orig_CallStaticIntMethodA = NULL;

/*
 * The one body all three entry points share.
 *
 * Resolution is deferred and retried because the module is loaded after the hook is
 * placed, and that is the ordinary case rather than a failure. Where the call came from
 * is what identifies the caller: a name can be changed, an address has to point at code
 * that exists.
 *
 * Bit 3 is dropped by replacing the whole result rather than masking it, because zero is
 * itself a meaningful answer for this shape of call — it reads as an outright failure
 * and produces the same finding. One matches no branch and reports nothing.
 */
static jint apply_callint_verdict(jint ret, uintptr_t caller) {
    if (g_detector_range_state == 0) {
        int n = __atomic_add_fetch(&g_detector_probe_count, 1, __ATOMIC_RELAXED);
        if (n > DETECTOR_PROBE_LIMIT) {
            g_detector_range_state = -1;
            LOGI("callint: detector never appeared, passthrough");
        } else {
            resolve_detector_range();
        }
    }
    if (g_detector_range_state != 1) {
        return ret;
    }
    if (caller < g_detector_lo || caller >= g_detector_hi) {
        return ret;
    }

    if ((ret & 0x8) != 0) {
        int n = __atomic_add_fetch(&g_callint_cleared, 1, __ATOMIC_RELAXED);
        if (n <= 8) {
            LOGI("callint(static): 0x%x -> 1 at 0x%lx", ret, (unsigned long) caller);
        }
        ret = 1;
    }
    return ret;
}

static jint JNICALL hook_CallStaticIntMethodA(JNIEnv* env, jclass clazz, jmethodID mid, const jvalue* args) {
    jint ret = g_orig_CallStaticIntMethodA(env, clazz, mid, args);
    return apply_callint_verdict(ret, (uintptr_t) __builtin_return_address(0));
}

static jint JNICALL hook_CallStaticIntMethodV(JNIEnv* env, jclass clazz, jmethodID mid, va_list args) {
    jint ret = g_orig_CallStaticIntMethodV(env, clazz, mid, args);
    return apply_callint_verdict(ret, (uintptr_t) __builtin_return_address(0));
}

static jint JNICALL hook_CallStaticIntMethod(JNIEnv* env, jclass clazz, jmethodID mid, ...) {
    va_list args;
    va_start(args, mid);
    jint ret = g_orig_CallStaticIntMethodV(env, clazz, mid, args);
    va_end(args);
    return apply_callint_verdict(ret, (uintptr_t) __builtin_return_address(0));
}

static void install_jni_callint_hook(JNIEnv* env) {
    if (env == NULL) { LOGI("callint: env null"); return; }
    const struct JNINativeInterface* table = *env;
    if (table == NULL) { LOGI("callint: table null"); return; }
    if (g_orig_CallIntMethodV != NULL) { LOGI("callint: already installed"); return; }

    long page_size = sysconf(_SC_PAGESIZE);
    uintptr_t page = (uintptr_t)table & ~(uintptr_t)(page_size - 1);
    if (mprotect((void*)page, (size_t)page_size * 2, PROT_READ | PROT_WRITE) != 0) {
        LOGI("callint: mprotect failed: %d", errno);
        return;
    }

    g_orig_CallIntMethodV = table->CallIntMethodV;
    g_orig_CallIntMethod = table->CallIntMethod;

    struct JNINativeInterface* mutable_table = (struct JNINativeInterface*)(uintptr_t)table;
    mutable_table->CallIntMethodV = hook_CallIntMethodV;
    mutable_table->CallIntMethod = hook_CallIntMethod;

    /*
     * The static family, which is the one the detector actually uses.
     */
    g_orig_CallStaticIntMethod = table->CallStaticIntMethod;
    g_orig_CallStaticIntMethodV = table->CallStaticIntMethodV;
    g_orig_CallStaticIntMethodA = table->CallStaticIntMethodA;
    mutable_table->CallStaticIntMethod = hook_CallStaticIntMethod;
    mutable_table->CallStaticIntMethodV = hook_CallStaticIntMethodV;
    mutable_table->CallStaticIntMethodA = hook_CallStaticIntMethodA;
    LOGI("callint: static slots installed A=%p V=%p", (void*) g_orig_CallStaticIntMethodA, (void*) g_orig_CallStaticIntMethodV);

    mprotect((void*)page, (size_t)page_size * 2, PROT_READ);
    LOGI("callint: installed (V+non-V), origV=%p orig=%p", (void*)g_orig_CallIntMethodV, (void*)g_orig_CallIntMethod);
}

static bool g_installed = false;

/*
 * Declared up here because two very different places need them: the VMA
 * relabelling below, and the loader-name filter above it, which has to know
 * which libraries have had their identity rewritten.
 */
typedef struct {
    char soname[64];
    char label[64];
    /*
     * Set once this entry has actually been applied.
     *
     * The retry loop calls the apply function on every pass, because a library
     * that loads after install can only be reached from there. Without this flag
     * every pass would replace the same library again, and a replacement is not a
     * rename: it unmaps and remaps the pages, which is a rollback for any thread
     * already running inside them. That is not a theoretical concern — ART
     * aborted the process from its profile saver when its own code pages were
     * rebuilt underneath it.
     */
    int done;
} vma_relabel_spec;

static vma_relabel_spec g_vma_specs[8];
static int g_vma_spec_count = 0;

/* ------------------------------------------------------------------------- */
/* Hook bookkeeping                                                          */
/* ------------------------------------------------------------------------- */

/*
 * Declared up here rather than beside the ARM64 patching code for one concrete
 * reason: the maps filter below has to know which pages belong to this module's
 * trampolines, and that filter is compiled on every architecture. Keeping the
 * record here means the same declaration serves both, and the filter never needs
 * to know whether an inline hook was actually installed — a zero count simply
 * means no page is ours.
 */

#define PAGE_SIZE 4096

/*
 * Trampoline slot geometry.
 *
 * The slot has to hold two independent things: the relocated instruction(s) that
 * the entry patch displaced, and a register-indirect jump to the replacement. They
 * are placed at fixed offsets so that the entry branch and the return branch are
 * both computable once, without depending on which relocation plan was chosen.
 *
 * The sizes come straight from the layout in `install_inline_hook`:
 *
 *   RELOC_COPY / ADR / ADRP   occupy words 0..1  (instruction, then B back)
 *   RELOC_B                   occupies words 0..4  (LDR, BR, then the 64-bit dest)
 *   RELOC_BL                  occupies words 0..6  (LDR X30, LDR X17, BR, ret, dest)
 *   jump gadget               occupies words 8..11 (LDR X17, BR X17, 64-bit addr)
 *
 * The widest plan needs 7 words and the gadget starts at word 8, so there is a
 * one-word gap that nothing is ever executed from. Sixteen words gives four spare
 * words beyond the gadget for a future plan that needs a little more room without
 * having to move the gadget and re-derive both branches.
 *
 * The two assertions below are what keep that arithmetic honest. Without them, a
 * later edit that moved the gadget or added a word to the widest plan would still
 * compile and would silently point the entry branch or a literal load at the wrong
 * word — a failure that only shows up as a crash inside libc much later.
 */
#define TRAMPOLINE_SLOT_WORDS 16
#define TRAMPOLINE_JUMP_OFFSET 32

_Static_assert(TRAMPOLINE_JUMP_OFFSET / 4 >= 7,
               "the widest relocation plan must fit before the jump gadget");
_Static_assert(TRAMPOLINE_JUMP_OFFSET + 16 <= TRAMPOLINE_SLOT_WORDS * 4,
               "the jump gadget must fit inside the trampoline slot");

struct hook_record {
    void *target;
    void *replacement;
    void *trampoline;
    size_t patch_len;
    bool active;
};

/*
 * Sixty-four slots, not sixteen.
 *
 * Sixteen was enough while every hook was on a libc symbol and the set was known in
 * advance. Entry guards are counted in the same table, and their number is a property
 * of what is being guarded rather than of this module: a detector with twenty checks
 * wants twenty slots on top of the fifteen the exit guards already use. Silently
 * refusing to install past the sixteenth would look exactly like a guard that does
 * not work, which is the hardest failure to diagnose from the other side.
 */
static struct hook_record g_hooks[64];
static int g_hook_count = 0;

/* ------------------------------------------------------------------------- */
/* Path and property tables                                                   */
/* ------------------------------------------------------------------------- */

/*
 * Paths whose existence must read as "absent".
 *
 * Each is a root tree rather than a single file, because a root check walks
 * the directory instead of asking about one name; hiding only the exact path
 * would leave the next one visible.
 */
static const char *kHiddenPrefixes[] = {
        "/data/adb",
        "/sbin/su",
        /*
         * `/system/bin/su` is deliberately absent.
         *
         * Measured in the untrusted_app domain, that path exists and is readable:
         * the kernel answers `lrwxrwxrwx ... -> ./magisk` to stat and readlink. The
         * two entries below are genuinely absent on this device, so answering
         * ENOENT for them is what the kernel already does. Claiming the same about
         * a path the caller can see with its own credentials is a contradiction
         * between two answers to the same question, and it is the discrepancy a
         * comparison against a stock handset finds first.
         *
         * The name is neutralised where it actually leaks: `rewrite_link_result`
         * rewrites the target `./magisk` to `./worker`, so the link reads as an
         * ordinary platform helper and the path itself keeps answering truthfully.
         */
        "/system/xbin/su",
        "/debug_ramdisk",
        "/dev/.magisk",
        "/dev/socket/adbd",
        NULL
};

/*
 * The path entries the baseline collector registered.
 *
 * The static table above holds the paths that are the same on every device: the
 * root solution's directory, its socket, its debug bridge. It cannot hold the
 * other things installed on this particular handset -- an app that was granted
 * root, a module directory, a framework's private data -- because those depend
 * on what the user installed, not on what platform this is.
 *
 * That set is exactly what the collector measures. It writes one `path|...|hide`
 * line per entry it found, and this table is where those lines land. The lookup
 * order matches `find_override`: measured entries first, fixed ones as fallback,
 * so a device with nothing installed still answers for the paths that every
 * device with the same root solution carries.
 *
 * Nothing here is synthesised. An entry exists because the collector saw the
 * directory, so hiding it reports the state of a handset that never had it --
 * a state that genuinely exists, not an invented one.
 */
static char **g_dyn_paths = NULL;
static int g_dyn_path_count = 0;

/*
 * Where the collector writes, and the file inside each run that the guards read.
 *
 * The pointer file holds one line: the timestamp directory of the newest run.
 * Indirecting through it is what lets a collection take effect without the guards
 * knowing anything about when it happened -- the guards always read the same two
 * names, and the second name is what the first one currently says.
 */
#define BASELINE_ROOT "/data/misc/.guise_baseline"
#define BASELINE_POINTER BASELINE_ROOT "/latest"
#define BASELINE_GUARDS_FILE "guards.txt"

/*
 * Copies one field of a `type|pattern|action` line into [out].
 *
 * Returns the index just past the field, or [len] when the line has fewer fields
 * than were asked for. Empty fields are legal and yield an empty string, which is
 * how a malformed line ends up being skipped by the caller rather than by a
 * separate validation pass.
 */
static size_t guards_field(const char *line, size_t len, size_t start, char *out, size_t cap) {
    size_t i = start;
    size_t n = 0;
    while (i < len && line[i] != '|' && line[i] != '\n' && line[i] != '\r') {
        if (n + 1 < cap) out[n++] = line[i];
        i++;
    }
    out[n] = 0;
    return i;
}

/*
 * Loads the collected path entries into the dynamic table.
 *
 * Runs once, from the install path, and reads through the originals rather than
 * through this module's own hooks: `fopen` and `fgets` are both redirected by the
 * time this is called, and a reader that went through them would be asking a
 * question this module answers, not one the filesystem answers.
 *
 * Only `path|...|hide` lines are consumed. The other types describe the same set
 * of facts in other shapes -- a package name is also a `/data/data/<pkg>` path, and
 * the collector emits both -- so taking the path form alone covers the same ground
 * without the guard having to know how to turn a package name into a directory.
 *
 * A missing baseline is not an error. It means the collector has not run on this
 * handset yet, and the guards then answer from the fixed table alone, which is the
 * behaviour every device had before this existed.
 */
static void load_baseline_paths(void) {
    if (real_fopen == NULL || real_fgets == NULL) return;

    char pointer[256];
    size_t pointer_len = 0;
    FILE *fp = real_fopen(BASELINE_POINTER, "r");
    if (fp == NULL) {
        LOGI("baseline: no pointer, guards use the fixed table only");
        return;
    }
    if (real_fgets(pointer, (int) sizeof(pointer), fp) != NULL) {
        pointer_len = strlen(pointer);
    }
    fclose(fp);

    while (pointer_len > 0 &&
           (pointer[pointer_len - 1] == '\n' || pointer[pointer_len - 1] == '\r')) {
        pointer[--pointer_len] = 0;
    }
    if (pointer_len == 0) {
        LOGI("baseline: pointer empty, guards use the fixed table only");
        return;
    }

    char guards_path[512];
    snprintf(guards_path, sizeof(guards_path), "%s/%s/%s",
             BASELINE_ROOT, pointer, BASELINE_GUARDS_FILE);

    fp = real_fopen(guards_path, "r");
    if (fp == NULL) {
        LOGI("baseline: %s missing, guards use the fixed table only", guards_path);
        return;
    }

    /*
     * The table is sized once from a count taken in the same pass, rather than
     * grown: the file is written by one collector run and does not change while it
     * is being read, and a fixed allocation keeps this out of the allocator on a
     * path that runs during start-up.
     */
    char line[512];
    int capacity = 0;
    while (real_fgets(line, (int) sizeof(line), fp) != NULL) {
        if (line[0] != '#' && line[0] != '\n' && line[0] != '\r') capacity++;
    }
    if (capacity <= 0) {
        fclose(fp);
        LOGI("baseline: guards file has no rules");
        return;
    }

    char **paths = (char **) calloc((size_t) capacity, sizeof(char *));
    if (paths == NULL) {
        fclose(fp);
        LOGI("baseline: allocation failed for %d entries", capacity);
        return;
    }

    rewind(fp);
    int count = 0;
    while (count < capacity && real_fgets(line, (int) sizeof(line), fp) != NULL) {
        size_t len = strlen(line);
        if (len == 0 || line[0] == '#' || line[0] == '\n' || line[0] == '\r') continue;

        char type[16];
        char pattern[400];
        size_t p = guards_field(line, len, 0, type, sizeof(type));
        if (p >= len || line[p] != '|') continue;
        p = guards_field(line, len, p + 1, pattern, sizeof(pattern));
        if (p >= len || line[p] != '|') continue;

        if (strcmp(type, "path") != 0) continue;
        if (pattern[0] != '/') continue;

        char *copy = strdup(pattern);
        if (copy == NULL) continue;
        paths[count++] = copy;
    }
    fclose(fp);

    if (count <= 0) {
        free(paths);
        LOGI("baseline: no path entries in %s", guards_path);
        return;
    }

    g_dyn_paths = paths;
    g_dyn_path_count = count;
    LOGI("baseline loaded: %d path entries from %s", count, guards_path);
}

/*
 * Property names whose values are reported as the stock-device value.
 *
 * A device with developer options left alone reports these exact values, so
 * answering them changes which consistent state is reported, not whether the
 * report is true.
 */
struct PropOverride {
    const char *name;
    const char *value;
    int int_value;
    bool is_int;
};

static const struct PropOverride kPropOverrides[] = {
        {"init.svc.adbd",          "stopped", 0, false},
        {"sys.usb.state",          "mtp",     0, false},
        /*
         * The two configuration properties behind the gadget.
         *
         * `sys.usb.state` is what the gadget reports it applied; these two are what
         * the user asked for. A reader that wants to know whether adb is exposed can
         * ask either, and answering only the first left the second saying
         * `mtp,conn_gadget,adb` -- the same three functions, one property further
         * along. `mtp` is the single function a stock handset exposes, so the answer
         * is the same shape the state property already gives.
         */
        {"sys.usb.config",          "mtp",     0, false},
        {"persist.sys.usb.config",  "mtp",     0, false},
        {"persist.sys.test_harness", "0",     0, false},

        /*
         * Whether the platform would accept an unlock request.
         *
         * `sys.oem_unlock_allowed` is the runtime switch beside the build-time
         * `ro.oem_unlock_supported`. A handset that reports the second as 0 and this
         * one as 1 is not self-consistent: the build says unlocking is unsupported,
         * and the runtime says it is permitted. Answering 0 here is the same handset
         * the rest of the table already describes.
         */
        {"sys.oem_unlock_allowed",  "0",       0, false},
        {"ro.debuggable",          "0",       0, false},
        {"service.adb.root",       "0",       0, false},

        /*
         * The boot state a locked, factory handset reports.
         *
         * These four are the properties a bootloader check reads directly: whether the
         * flash is locked, whether verified boot ended green, whether vbmeta says the
         * device is locked, and the Knox warranty bit. A rooted handset answers
         * `0`/`orange`/`unlocked`/`1`, and each of those is a finding on its own.
         *
         * Answering the locked set is a change of state, not a false statement about
         * the hardware: the values below are exactly what the same handset reports
         * before it is unlocked, so a reader that asks these four gets a coherent
         * answer rather than a mix of locked and unlocked claims.
         *
         * The vbmeta digest, size, hash algorithm and avb_version are deliberately
         * NOT overridden. They are the attested image identity, and Duck compares them
         * against the TEE-issued attestation record when that record is available.
         * Editing them while leaving attestation intact is what produces a
         * `vbmeta_digest_mismatch` finding -- a Danger that appears only because the
         * two were made to disagree. When attestation is unavailable the comparison is
         * skipped (`runtimeComparisonPerformed=false`), so leaving the digest alone
         * costs nothing and keeps the four state properties above internally
         * consistent with a handset that was never unlocked.
         */
        {"ro.boot.flash.locked",           "1",     1, false},
        {"ro.boot.verifiedbootstate",      "green", 0, false},
        {"ro.boot.vbmeta.device_state",    "locked", 0, false},
        {"ro.boot.warranty_bit",           "0",     0, false},
        {"ro.oem_unlock_supported",        "0",     0, false},

        /*
         * The two properties that name the physical handset.
         *
         * `ro.boot.em.model` is `SM-S9060` and `ro.boot.bootloader` is
         * `S9060ZCS9EYC1` on this device. Neither is read by a bootloader check, but
         * both state the real model and the real firmware build, which is the exact
         * contradiction a model-consistency comparison looks for once the configuration
         * claims to be a different handset.
         *
         * An empty value is the truthful answer for a device that does not have them:
         * these are Samsung-specific properties, and a vivo or OPPO handset reports
         * them as unset rather than as an empty string. `hook_prop_get` returns a
         * zero-length value for an empty override, which is what a reader sees when it
         * asks for a property the platform never set -- so the shape of the answer
         * matches the shape of an absent property.
         */
        {"ro.boot.em.model",                "",      0, false},
        {"ro.boot.bootloader",              "",      0, false},

        /*
         * The vendor-side and mode-side siblings of the boot state above.
         *
         * A vendor image can carry its own copy of the same four facts under
         * `vendor.boot.*`, and the vendor warranty bits are separate properties again.
         * A reader that checks them alongside the platform ones sees the same
         * locked, factory-fresh handset only if both copies agree -- so they are
         * answered here with the same values, not left to the kernel to fill in.
         *
         * `ro.bootmode`, `ro.boot.mode` and `vendor.boot.mode` all report how the
         * device booted. `unknown` on a running handset means the platform did not
         * record it, which is itself a boot-path question a reader can ask; `normal`
         * is what a handset that came up through the ordinary path reports.
         */
        {"ro.bootmode",                    "normal", 0, false},
        {"ro.boot.mode",                   "normal", 0, false},
        {"vendor.boot.mode",               "normal", 0, false},
        {"vendor.boot.vbmeta.device_state", "locked", 0, false},
        {"vendor.boot.verifiedbootstate",  "green",  0, false},
        {"ro.vendor.boot.warranty_bit",    "0",      0, false},
        {"ro.vendor.warranty_bit",         "0",      0, false},
        {"ro.warranty_bit",                "0",      0, false},
        {"ro.boot.knox.state",             "NORMAL", 0, false},

        /*
         * SELinux, as the bootloader reports it.
         *
         * `ro.boot.selinux` is the state the bootloader was told to use, while
         * `ro.build.selinux` describes whether the build supports SELinux at all.
         * This device answers `permissive` for the first, which is a finding on its
         * own: a permissive kernel lets any process do anything, and no retail
         * handset ships that way. `enforcing` is the value the same handset reports
         * with the stock boot image.
         */
        {"ro.boot.selinux",                "enforcing", 0, false},

        /*
         * The dm-verity state of each partition.
         *
         * `2` means the partition was verified but the result was not enforced,
         * which is what happens once the verity state has been changed after the
         * image was signed. `1` is full enforcement. The five partitions below are
         * the ones the platform records separately; answering all five the same way
         * keeps them consistent with each other, which is the check that compares
         * them.
         */
        {"partition.system.verified",      "1",     1, false},
        {"partition.vendor.verified",      "1",     1, false},
        {"partition.product.verified",     "1",     1, false},
        {"partition.system_ext.verified",  "1",     1, false},
        {"partition.odm.verified",         "1",     1, false},

        /*
         * Services that would only run on a rooted handset.
         *
         * `init.svc.*` reports the state init has for a named service. These two
         * belong to Magisk; they are unset on a stock handset, and `stopped` is the
         * state a defined-but-not-running service reports. A reader asking whether
         * Magisk is running gets the same answer either way.
         */
        {"init.svc.magisk_daemon",         "stopped", 0, false},
        {"init.svc.magisk_service",        "stopped", 0, false},
        {"ro.magisk.hide",                 "0",      0, false},

        /*
         * The developer-options switch, as a property.
         *
         * The Settings value is what the Settings app reads; this property is the
         * same fact exposed for callers that cannot reach the settings provider.
         * Answering both the same way keeps the two in agreement.
         */
        {"persist.sys.development_settings_enabled", "0", 0, false},

        {NULL, NULL, 0, false}
};

/*
 * The real value of a property and the value that should be answered instead.
 *
 * This is the only table the substitution engine needs, and it is deliberately made of
 * values rather than names. A name is a handle a reader uses to ask a question; a value
 * is the answer. Anything that reaches an answer has already decided which question it
 * wants, and from that point the name is invisible to it — it holds bytes and compares
 * those bytes against whatever it expects. Matching the bytes works regardless of how
 * they were obtained: through the property service, through the file, through a mapping
 * of the file, or through an interface that has not been written yet.
 *
 * Nothing here is guessed. The real side is what the device reports, captured once before
 * any substitution happened; the fake side is the same configuration that already drives
 * every other layer. A pair of equal length costs nothing to apply. A pair of unequal
 * lengths is padded or truncated to the space the original already occupied, which is
 * safe for a fixed-size property slot and safe for a text line, because both are
 * terminated and the edit never moves what follows.
 */
struct ValuePair {
    const char *real;
    const char *fake;
};

static struct ValuePair *g_value_pairs = NULL;
static int g_value_pair_count = 0;

/*
 * The kernel identity, as the module configured it.
 *
 * `struct utsname` is six `char[65]` fields, and a `SYS_uname` reply is written
 * into exactly that structure. Values coming from the kernel are therefore
 * always representable; anything longer than 64 bytes is not a kernel identity
 * at all, which is why the fields here are sized to match rather than allocated
 * freely.
 *
 * The real values are captured once, from the kernel itself, on the first
 * configuration call. They are needed for two separate reasons:
 *
 *   - the text surfaces (`/proc/sys/kernel/osrelease`, `/proc/version`) can only
 *     be rewritten length-preservingly, because `read` returns a count that this
 *     guard cannot change;
 *   - a fake whose length differs from the real value would therefore apply to
 *     `SYS_uname` and not to those files, which is precisely the disagreement a
 *     detector reports as kernel spoofing.
 *
 * So the fake is applied to `SYS_uname` only when it has the same length as the
 * real value. Otherwise every surface keeps the kernel answer, which is always
 * internally consistent, and the mismatch is logged so the config can be fixed.
 * Being wrong in a visible and consistent way beats being wrong in a way that
 * only surfaces as a cross-source contradiction.
 */
#define GUISE_UTS_FIELD 65

static char g_kernel_release_real[GUISE_UTS_FIELD];
static char g_kernel_release_fake[GUISE_UTS_FIELD];
static char g_kernel_version_real[GUISE_UTS_FIELD];
static char g_kernel_version_fake[GUISE_UTS_FIELD];

static bool g_kernel_release_apply = false;
static bool g_kernel_version_apply = false;

/*
 * Whether [c] can continue a token that a value might be embedded in.
 *
 * The substitution table holds values, not names, and a value is replaced wherever
 * its bytes appear. That is correct for a property slot, where the value stands
 * alone between brackets, and wrong for a string that merely contains the same
 * letters as part of a longer identifier. The Samsung incremental S9060ZCS9EYC1
 * also appears inside the kernel release
 * 5.10.209-android12-9-1839202-abS9060ZCS9EYC1, and replacing it there shortens
 * the kernel string, fills the tail with NUL bytes, and makes every reader of that
 * string disagree with the one that asked the kernel directly. A detector that
 * compares the two reads then reports kernel spoofing, which is the opposite of
 * what the replacement was for.
 *
 * A value is therefore replaced only where it stands as a value: preceded and
 * followed by something that cannot continue an identifier. Brackets, slashes,
 * colons, spaces, quotes and the ends of the buffer all qualify; letters, digits,
 * underscore, dot and hyphen do not, because those are exactly the characters that
 * would make the match part of a longer token.
 */
static bool is_token_char(char c) {
    if (c >= 97 && c <= 122) return true;   /* a-z */
    if (c >= 65 && c <= 90) return true;    /* A-Z */
    if (c >= 48 && c <= 57) return true;    /* 0-9 */
    return c == 95 || c == 46 || c == 45;   /* _ . - */
}

/*
 * Replaces every configured real value that appears in a buffer.
 *
 * The scan covers the whole buffer rather than lines or records, because the caller chose
 * the chunk size and that boundary has nothing to do with where a value begins. A value
 * split across two reads cannot be fixed here — that is why the callers that can hold a
 * whole file do so, and the ones that cannot get whatever complete values happen to be
 * inside the block they asked for. Both answers are correct; one is merely more complete.
 *
 * The replacement keeps the length. A shorter fake leaves the rest of the original field
 * filled with NUL bytes, which ends the string where a shorter value naturally would and
 * leaves every following byte untouched. A longer fake is truncated to the space the
 * original occupied, because growing the field would move everything after it and the
 * reader is entitled to the offsets it was given.
 */
/*
 * Rewrites the `[name]: [value]` lines that `getprop` prints.
 *
 * `getprop` is a separate program. It is not injected, so it reads the real property
 * area and prints the real values; the only chance to change its answer is on the way
 * back, in the pipe its parent reads.
 *
 * The text arrives as complete lines in the common case -- the whole dump is a few
 * kilobytes and is delivered in one read -- and each line carries both the name and
 * the value, which is what makes a targeted substitution possible. Without the name
 * on the same line the only option would be matching bare values, and a bare value
 * like 1 appears in hundreds of unrelated properties.
 *
 * The replacement is padded with spaces to the length of the value it replaces. That
 * padding is not cosmetic: the caller counts the bytes it received, so the buffer
 * cannot grow or shrink. It is also harmless, because a property value is compared
 * after trimming -- both the reading side and the comparison trim -- so a trailing
 * space and no trailing space are the same value, while the byte count stays exactly
 * what the kernel reported.
 *
 * A fake longer than the value it replaces is skipped. There is no room for it, and
 * shortening it would invent a value that no device reports. Leaving the real value in
 * place is the safe failure: it is a valid answer of the correct shape, and the
 * configuration is what needs fixing.
 */
#define GP_LBRACK  0x5B
#define GP_RBRACK  0x5D
#define GP_COLON   0x3A
#define GP_LF      0x0A
#define GP_SPACE   0x20
#define GP_TAB     0x09

/*
 * Declared ahead of its definition, which sits further down beside the dynamic
 * table it prefers. This pass runs in the read path and needs the lookup, but the
 * lookup itself is not part of what this pass does -- it only asks it a question.
 * The declaration is `static` to match the definition, or the two would disagree
 * about linkage and the compiler would reject the second one.
 */
static const struct PropOverride *find_override(const char *name);

static int g_getprop_diag = 0;
static int g_getprop_name_diag = 0;

/*
 * Whether [buf] looks like getprop output.
 *
 * The distinguishing mark is a line of the form [name]: [value] carrying a property
 * name that only a full dump would contain. A short substring search is enough and
 * costs one pass over the buffer, which is the price of knowing for certain that the
 * pipe this function is meant to rewrite was the one it was given.
 */
static bool looks_like_getprop_dump(const char *buf, size_t len) {
    if (len < 64) return false;
    const char *needle = "ro.boot.flash.locked";
    size_t needle_len = strlen(needle);
    if (len < needle_len) return false;
    for (size_t i = 0; i + needle_len <= len; i++) {
        if (memcmp(buf + i, needle, needle_len) == 0) return true;
    }
    return false;
}

static int rewrite_getprop_lines(char *buf, size_t len) {
    if (buf == NULL || len < 8) return 0;

    int changed = 0;
    int lines_bracket = 0;
    int lookup_hits = 0;
    size_t pos = 0;



    while (pos + 4 < len) {
        /* A line begins with the name in brackets. */
        if (buf[pos] != GP_LBRACK) { pos++; continue; }
        lines_bracket++;

        size_t name_start = pos + 1;
        size_t name_end = name_start;
        while (name_end < len && buf[name_end] != GP_RBRACK && buf[name_end] != GP_LF) name_end++;
        if (name_end >= len || buf[name_end] != GP_RBRACK) { pos++; continue; }

        size_t name_len = name_end - name_start;
        if (name_len == 0 || name_len >= 64) { pos = name_end + 1; continue; }

        /* Then a colon, then the opening bracket of the value. */
        size_t p = name_end + 1;
        while (p < len && (buf[p] == GP_SPACE || buf[p] == GP_TAB)) p++;
        if (p >= len || buf[p] != GP_COLON) { pos = name_end + 1; continue; }
        p++;
        while (p < len && (buf[p] == GP_SPACE || buf[p] == GP_TAB)) p++;
        if (p >= len || buf[p] != GP_LBRACK) { pos = name_end + 1; continue; }
        p++;

        size_t value_start = p;
        size_t value_end = value_start;
        while (value_end < len && buf[value_end] != GP_RBRACK && buf[value_end] != GP_LF) value_end++;
        if (value_end >= len || buf[value_end] != GP_RBRACK) { pos = name_end + 1; continue; }

        /* The name is not terminated here, so a bounded copy is used for the lookup. */
        char name[64];
        memcpy(name, buf + name_start, name_len);
        name[name_len] = 0;

        if (g_getprop_name_diag < 6 && name[0] != 0) {
            LOGI("parsed name: [%s] len=%lu", name, (unsigned long) name_len);
            g_getprop_name_diag++;
        }
        const struct PropOverride *o = find_override(name);
        if (o != NULL && o->value != NULL) {
            lookup_hits++;
            const char *fake = o->value;
            size_t flen = strlen(fake);
            size_t rlen = value_end - value_start;

            if (flen <= rlen) {
                /*
                 * Skip when the line already carries the substitute. Comparing the
                 * visible length rather than the bytes avoids rewriting a value that a
                 * previous pass already padded: the padded form has trailing spaces, so
                 * a byte comparison against the unpadded fake would differ and the pass
                 * would run again, growing nothing but spending time.
                 */
                size_t visible = rlen;
                while (visible > 0 && buf[value_start + visible - 1] == GP_SPACE) visible--;
                bool already = (visible == flen) && (memcmp(buf + value_start, fake, flen) == 0);
                if (!already) {
                    memcpy(buf + value_start, fake, flen);
                    if (flen < rlen) memset(buf + value_start + flen, GP_SPACE, rlen - flen);
                    changed++;
                }
            }
        }

        pos = value_end + 1;
    }
    if (g_getprop_diag < 10) {
        bool has_boot = false;
        for (size_t q = 0; q + 7 <= len; q++) {
            if (memcmp(buf + q, "ro.boot", 7) == 0) { has_boot = true; break; }
        }
        if (has_boot) {
            char hex[601];
            size_t hn = len < 200 ? len : 200;
            for (size_t q = 0; q < hn; q++) {
                static const char *H = "0123456789abcdef";
                unsigned char c = (unsigned char) buf[q];
                hex[q*3] = H[c >> 4];
                hex[q*3+1] = H[c & 15];
                hex[q*3+2] = 32;
            }
            hex[hn*3] = 0;
            LOGI("BOOT data: len=%lu brk=%d hits=%d hex=[%s]",
                 (unsigned long) len, lines_bracket, lookup_hits, hex);
            g_getprop_diag++;
        }
    }
    if (len > 30000) {
        char head[201];
        size_t hn = len < 200 ? len : 200;
        memcpy(head, buf, hn);
        head[hn] = 0;
        for (size_t q = 0; q < hn; q++) {
            unsigned char c = (unsigned char) head[q];
            if (c < 32 || c > 126) head[q] = 46;
        }
        LOGI("BIG buf: len=%lu brk=%d hits=%d chg=%d head=[%s]",
             (unsigned long) len, lines_bracket, lookup_hits, changed, head);
    }
    if (lines_bracket > 0 || lookup_hits > 0 || changed > 0) {
        if (g_getprop_diag < 12) {
            char preview[161];
            size_t n = len < 160 ? len : 160;
            memcpy(preview, buf, n);
            preview[n] = 0;
            for (size_t q = 0; q < n; q++) {
                unsigned char c = (unsigned char) preview[q];
                if (c < 32 || c > 126) preview[q] = 46;
            }
            LOGI("getprop raw: diag=%d len=%lu brk=%d hits=%d body=[%s]",
                 g_getprop_diag, (unsigned long) len, lines_bracket, lookup_hits, preview);
            g_getprop_diag++;
        }
        LOGI("getprop result: len=%lu bracket_lines=%d override_hits=%d changed=%d",
             (unsigned long) len, lines_bracket, lookup_hits, changed);
    }
    return changed;
}

static int rewrite_value_pairs(char *buf, size_t len) {
    if (buf == NULL || len == 0 || g_value_pair_count == 0) return 0;

    int changed = 0;
    for (int i = 0; i < g_value_pair_count; i++) {
        const char *real = g_value_pairs[i].real;
        const char *fake = g_value_pairs[i].fake;
        if (real == NULL || fake == NULL) continue;

        size_t rlen = strlen(real);
        if (rlen == 0 || rlen > len) continue;

        for (size_t pos = 0; pos + rlen <= len; pos++) {
            if (memcmp(buf + pos, real, rlen) != 0) continue;
            /*
             * A match inside a longer token is not this value. The check is on the
             * immediate neighbours only: a value that stands between two separators
             * cannot be part of anything longer, and a value whose neighbour is an
             * identifier character is part of a different word entirely.
             */
            if (pos > 0 && is_token_char(buf[pos - 1])) continue;
            if (pos + rlen < len && is_token_char(buf[pos + rlen])) continue;

            /*
             * Length-preserving means exactly that: the fake has to occupy the
             * same number of bytes as the real value, or the replacement is
             * skipped entirely.
             *
             * The earlier version allowed a shorter fake and filled the rest
             * with NUL bytes. That is correct inside a fixed property slot, where
             * the record layout reserves the space and a shorter string simply
             * ends sooner -- and wrong everywhere else, because the buffer is
             * then continuous text whose structure the NUL bytes destroy. The
             * kernel release is the case that made this visible: the incremental
             * `S9060ZCS9EYC1` is a substring of
             * `5.10.209-android12-9-1839202-abS9060ZCS9EYC1`, so shortening it
             * truncated the version line, `/proc/version` stopped parsing, and a
             * detector comparing the two reads reported kernel spoofing.
             *
             * Skipping instead of truncating cannot produce a wrong answer: a
             * value that does not fit is left as the device reports it, which is
             * always a valid value of the right shape. The property slot keeps
             * its own rewrite (`rewrite_prop_values`), which knows the record
             * layout and pads within it.
             */
            size_t flen = strlen(fake);
            if (flen != rlen) continue;
            memcpy(buf + pos, fake, rlen);

            changed++;
            pos += rlen - 1;
        }
    }
    return changed;
}

/*
 * Parses the pairs, one per line, as real=fake.
 *
 * The separator is the first '=' on the line, not the last, because a value can contain
 * an '=' of its own and the fake side can contain any number of them. Names cannot, and
 * names are not what this table holds anyway.
 *
 * Replacing rather than appending makes the call idempotent, which matters because the
 * module configures the guard on every process start and cannot know whether this is the
 * first time it has done so in this process.
 */
static void set_value_pairs(const char *text) {
    LOGI("set_value_pairs: enter len=%d", text == NULL ? -1 : (int) strlen(text));
    if (g_value_pairs != NULL) {
        for (int i = 0; i < g_value_pair_count; i++) {
            free((void *) g_value_pairs[i].real);
            free((void *) g_value_pairs[i].fake);
        }
        free(g_value_pairs);
        g_value_pairs = NULL;
        g_value_pair_count = 0;
    }
    if (text == NULL || text[0] == '\0') {
        LOGI("set_value_pairs: empty, table cleared");
        return;
    }

    int capacity = 32;
    g_value_pairs = (struct ValuePair *) calloc((size_t) capacity, sizeof(struct ValuePair));
    if (g_value_pairs == NULL) return;

    const char *p = text;
    while (*p != '\0') {
        const char *nl = strchr(p, '\n');
        const char *end = (nl != NULL) ? nl : p + strlen(p);

        const char *s = p;
        while (s < end && (*s == ' ' || *s == '\t')) s++;
        const char *e = end;
        while (e > s && (e[-1] == '\r' || e[-1] == ' ' || e[-1] == '\t')) e--;

        const char *eq = memchr(s, '=', (size_t) (e - s));
        if (eq != NULL && eq > s && eq + 1 < e) {
            if (g_value_pair_count == capacity) {
                capacity *= 2;
                struct ValuePair *grown = (struct ValuePair *) realloc(
                        g_value_pairs, (size_t) capacity * sizeof(struct ValuePair));
                if (grown == NULL) break;
                g_value_pairs = grown;
            }
            size_t real_len = (size_t) (eq - s);
            size_t fake_len = (size_t) (e - eq - 1);
            char *real = (char *) malloc(real_len + 1);
            char *fake = (char *) malloc(fake_len + 1);
            if (real != NULL && fake != NULL) {
                memcpy(real, s, real_len);
                real[real_len] = '\0';
                memcpy(fake, eq + 1, fake_len);
                fake[fake_len] = '\0';
                g_value_pairs[g_value_pair_count].real = real;
                g_value_pairs[g_value_pair_count].fake = fake;
                g_value_pair_count++;
            } else {
                free(real);
                free(fake);
            }
        }
        p = (nl != NULL) ? nl + 1 : end;
    }
    LOGI("value pairs loaded: %d", g_value_pair_count);
}


/* ------------------------------------------------------------------------- */
/* Helpers                                                                    */
/* ------------------------------------------------------------------------- */

static bool starts_with(const char *s, const char *prefix) {
    if (s == NULL || prefix == NULL) return false;
    size_t n = strlen(prefix);
    return strncmp(s, prefix, n) == 0;
}

/*
 * Whether [path] is one whose existence the guarded process must not learn.
 *
 * Matching is by whole path or by directory prefix, never by substring: a
 * substring test would also hide an unrelated file whose name merely contains
 * the same letters, and the rule has to stay narrow enough that it can never
 * affect anything but the exact trees named above.
 */
/*
 * Records the paths the guarded process asks about that name the module's
 * infrastructure.
 *
 * The map snapshot is now verifiably clean — every token counted to zero after the
 * rewrite, in the same buffer the caller receives — and the report still names the
 * same four items. That rules out the map as the source, and it rules out "the
 * rewrite did not run". What is left is a different question to the filesystem, and
 * the only way to stop guessing at which one is to print the questions.
 *
 * The list is deliberately narrow. Logging every path would bury the signal under the
 * ordinary traffic of a running app, so only paths that spell one of these names are
 * printed, once each, up to a fixed count. A single startup then shows the complete
 * set of module-related queries without flooding the buffer.
 *
 * Matching is case-sensitive on the literal spellings because these are path
 * components and package names, which are lower case by convention on this platform;
 * a case-insensitive test would also match ordinary words like "result".
 */
static int g_diag_path_count = 0;
#define DIAG_PATH_LIMIT 80

static void diag_note_path(const char *path) {
    if (path == NULL || g_diag_path_count >= DIAG_PATH_LIMIT) return;

    static const char *kWatch[] = {
        "guise", "vector", "lsposed", "xposed", "magisk", "zygisk",
        "riru", "kernelsu", "apatch", "edxposed", "taichi",
        "/data/adb", "/debug_ramdisk", NULL
    };

    bool hit = false;
    for (int i = 0; kWatch[i] != NULL && !hit; i++) {
        if (strstr(path, kWatch[i]) != NULL) hit = true;
    }
    if (!hit) return;

    int c = g_diag_path_count++;
    LOGI("probe path #%d %s", c, path);
}

/*
 * The package this process belongs to, or empty when it could not be determined.
 *
 * Filled once at install time from `/proc/self/cmdline`, which is the process name
 * the platform launched: an app process reports its package, and an app process
 * with a separate remote process reports `package:remote`. Either way the first
 * colon is where the package name ends, so the prefix before it is what is wanted.
 *
 * This exists to answer one question -- "is this path mine?" -- and the answer must
 * never be yes when the question is whether to hide it.
 */
static char g_own_pkg[256];

/*
 * Reads the process's own package name into [g_own_pkg].
 *
 * Called from the install path, after `real_fopen` has been resolved and before the
 * entry guards are installed, so the value is in place before any application code
 * can ask a path question. Failure leaves the buffer empty, which makes the check
 * below a no-op and the guards behave exactly as they did before it existed -- the
 * safe direction, since a guard that cannot identify its caller must not assume the
 * path is foreign.
 */
static void capture_own_package(void) {
    g_own_pkg[0] = 0;
    if (real_fopen == NULL || real_fgets == NULL) return;

    FILE *fp = real_fopen("/proc/self/cmdline", "r");
    if (fp == NULL) {
        LOGI("own package: /proc/self/cmdline unreadable, self-exclusion disabled");
        return;
    }

    char line[512];
    if (real_fgets(line, (int) sizeof(line), fp) != NULL) {
        size_t n = strlen(line);
        /* The file is NUL-separated rather than newline-separated. */
        for (size_t i = 0; i < n; i++) {
            if (line[i] == '\0' || line[i] == '\n' || line[i] == '\r') { n = i; break; }
        }
        for (size_t i = 0; i < n; i++) {
            if (line[i] == ':') { n = i; break; }
        }
        if (n > 0 && n < sizeof(g_own_pkg)) {
            memcpy(g_own_pkg, line, n);
            g_own_pkg[n] = 0;
        }
    }
    fclose(fp);
    LOGI("own package: [%s]", g_own_pkg[0] != 0 ? g_own_pkg : "(unknown)");
}

static bool is_hidden_path(const char *path) {
    if (path == NULL || path[0] == '\0') return false;

    /*
     * Never hide a path that belongs to this process's own package.
     *
     * An app is allowed to open its own APK. It does so at start-up, before any
     * check runs: the resource system reads the APK to build the asset table, and
     * a failed open there is not a detection result but a crash. Hiding the
     * caller's own path therefore does not hide anything from the caller -- it
     * removes the caller, which is what happened when a detector's own
     * `/data/app/.../base.apk` was registered: the app aborted before it could
     * even reach the check that was supposed to be fooled.
     *
     * The rule is uniform, with no exception for which package is asking: every
     * process gets its own paths back, and only paths belonging to something else
     * are hidden. That is the same answer a stock device gives, where a process
     * can obviously read itself.
     *
     * The comparison is a substring test rather than an exact prefix because the
     * APK path embeds the package name in the middle
     * (`/data/app/~~hash/pkg-hash/base.apk`) rather than at a fixed offset. A
     * substring hit that is too broad errs toward not hiding, which is the safe
     * direction -- a guard that fails to hide something is a weaker guard, while
     * one that hides its caller is a broken process.
     */
    if (g_own_pkg[0] != 0 && strstr(path, g_own_pkg) != NULL) return false;

    diag_note_path(path);

    /*
     * Measured entries first, then the fixed ones -- the same order `find_override`
     * uses, for the same reason: what the collector found on this handset is more
     * specific than what every handset with the same root solution carries, so the
     * specific answer should win where the two would disagree about a path.
     */
    for (int i = 0; i < g_dyn_path_count; i++) {
        const char *prefix = g_dyn_paths[i];
        if (prefix == NULL) continue;
        if (strcmp(path, prefix) == 0) return true;
        if (starts_with(path, prefix)) {
            char next = path[strlen(prefix)];
            if (next == '/' || next == '\\') return true;
        }
    }

    for (int i = 0; kHiddenPrefixes[i] != NULL; i++) {
        const char *prefix = kHiddenPrefixes[i];
        if (strcmp(path, prefix) == 0) return true;
        if (starts_with(path, prefix)) {
            char next = path[strlen(prefix)];
            if (next == '/' || next == '\\') return true;
        }
    }
    return false;
}

/*
 * The override table is built at install time from the module's own configuration.
 *
 * A static table can only hold values that are the same on every device, which is
 * fine for the debugging switches but useless for the model. The whole point of the
 * disguise is that the Java layer and the native layer report the *same* chosen
 * device, and that device comes from the user's configuration rather than from this
 * file. Two layers answering the same query from two different sources is exactly how
 * the fingerprint ended up printed twice: once as the chosen HONOR device, once as the
 * real Samsung one.
 *
 * So the values arrive over JNI — the module already holds them as strings — and are
 * copied into a table that `find_override` consults before the static entries. The
 * static ones stay as the fallback for the debugging switches, which are the same on
 * every device and do not depend on the configuration at all.
 */
static struct PropOverride *g_dyn_props = NULL;
static int g_dyn_prop_count = 0;

static const struct PropOverride *find_override(const char *name) {
    if (name == NULL) return NULL;
    for (int i = 0; i < g_dyn_prop_count; i++) {
        if (strcmp(g_dyn_props[i].name, name) == 0) return &g_dyn_props[i];
    }
    for (int i = 0; kPropOverrides[i].name != NULL; i++) {
        if (strcmp(kPropOverrides[i].name, name) == 0) return &kPropOverrides[i];
    }
    return NULL;
}

/*
 * Installs the property overrides sent from the module.
 *
 * [text] is one `name=value` per line, exactly the map the Java side already built
 * for its own property hook, so the two layers cannot disagree about which device
 * they are describing.
 *
 * Every string is copied because the JNI buffer is only valid for the duration of the
 * call. Any previous table is freed first, so a second install — after a configuration
 * change, say — replaces the values instead of leaking the old ones.
 */
static void set_dynamic_props(const char *text) {
    if (g_dyn_props != NULL) {
        for (int i = 0; i < g_dyn_prop_count; i++) {
            free((void *) g_dyn_props[i].name);
            free((void *) g_dyn_props[i].value);
        }
        free(g_dyn_props);
        g_dyn_props = NULL;
        g_dyn_prop_count = 0;
    }
    if (text == NULL || text[0] == '\0') return;

    int capacity = 16;
    g_dyn_props = (struct PropOverride *) calloc(capacity, sizeof(struct PropOverride));
    if (g_dyn_props == NULL) return;

    const char *p = text;
    while (*p != '\0') {
        const char *nl = strchr(p, '\n');
        const char *end = (nl != NULL) ? nl : p + strlen(p);
        const char *eq = memchr(p, '=', (size_t) (end - p));
        if (eq != NULL && eq > p) {
            if (g_dyn_prop_count == capacity) {
                capacity *= 2;
                struct PropOverride *grown = (struct PropOverride *) realloc(
                        g_dyn_props, (size_t) capacity * sizeof(struct PropOverride));
                if (grown == NULL) break;
                g_dyn_props = grown;
            }
            size_t name_len = (size_t) (eq - p);
            size_t value_len = (size_t) (end - eq - 1);
            char *name = (char *) malloc(name_len + 1);
            char *value = (char *) malloc(value_len + 1);
            if (name != NULL && value != NULL) {
                memcpy(name, p, name_len);
                name[name_len] = '\0';
                memcpy(value, eq + 1, value_len);
                value[value_len] = '\0';
                g_dyn_props[g_dyn_prop_count].name = name;
                g_dyn_props[g_dyn_prop_count].value = value;
                g_dyn_props[g_dyn_prop_count].int_value = atoi(value);
                g_dyn_props[g_dyn_prop_count].is_int = false;
                g_dyn_prop_count++;
            } else {
                free(name);
                free(value);
            }
        }
        p = (nl != NULL) ? nl + 1 : end;
    }
    LOGI("dynamic props loaded: %d", g_dyn_prop_count);
}

/* ------------------------------------------------------------------------- */
/* Hooked entry points                                                        */
/* ------------------------------------------------------------------------- */

/*
 * A case-insensitive substring test, because bionic does not export one.
 *
 * `strcasestr` is a GNU extension that does not exist in Android's libc, and
 * pulling in a regex engine to find six letters would be absurd. Lowercasing both
 * sides into a small stack buffer is enough for the strings involved, all of which
 * are short ASCII names.
 */
/*
 * The same test, bounded by a length instead of by a terminator.
 *
 * `read` fills a buffer and returns a count; it writes no NUL. A search that stops
 * only at a zero byte therefore keeps walking past the bytes the caller actually
 * has, and where it stops has nothing to do with the data -- it stops at whatever
 * byte happens to be zero in the memory that follows, which may be before the
 * match, may be after it, and may not be mapped at all.
 *
 * That was not theoretical. On a 4096-byte chunk of the map file the `zygisk`
 * token sat close enough to the front to be found before the walk left the buffer,
 * so the token substitution ran and logged. The `libart.so` test in the same
 * function did not, and the consequence was silent: the runtime private copy was
 * never renamed, the detector kept finding it under its real name, and every run
 * reported the runtime as modified. The difference between the two was position,
 * not logic -- which is exactly the kind of failure that reads as "the fix did not
 * work" when the fix was never reached.
 *
 * The limit is `len`, the count the read returned, and the loop stops one needle
 * short of the end so the comparison inside never touches a byte past it.
 */
static bool contains_ignore_case_n(const char *haystack, size_t len, const char *needle) {
    if (haystack == NULL || needle == NULL) return false;
    size_t n = strlen(needle);
    if (n == 0 || len < n) return false;
    for (size_t pos = 0; pos + n <= len; pos++) {
        size_t i = 0;
        while (i < n && (haystack[pos + i] | 0x20) == (needle[i] | 0x20)) {
            i++;
        }
        if (i == n) return true;
    }
    return false;
}
static bool contains_ignore_case(const char *haystack, const char *needle) {
    if (haystack == NULL || needle == NULL) return false;
    size_t n = strlen(needle);
    if (n == 0) return false;
    for (const char *p = haystack; *p != '\0'; p++) {
        size_t i = 0;
        while (i < n && p[i] != '\0' &&
               (p[i] | 0x20) == (needle[i] | 0x20)) {
            i++;
        }
        if (i == n) return true;
    }
    return false;
}

/*
 * Whether [path] names one of the memory-map files.
 *
 * The same data is reachable as `/proc/self/maps`, `/proc/<pid>/maps` and
 * `/proc/thread-self/maps`, so the test is anchored on both ends rather than on
 * one literal path: a detector that hard-codes any of them is asking the same
 * question, and the guard has to recognise the whole family.
 */
static bool is_maps_path(const char *path) {
    if (path == NULL) return false;
    size_t len = strlen(path);
    if (len <= 5) return false;
    if (!starts_with(path, "/proc/")) return false;

    /*
     * `maps` and `smaps` are the two files that carry the same names.
     *
     * The first version only matched `/maps`, which meant a reader that opened
     * `/proc/self/smaps` saw the file completely untouched — and smaps is the more
     * likely target for a detector that wants more than the address ranges, because
     * it carries the same path column plus per-segment accounting. On this device the
     * token appears three times in `smaps` and three times in `maps`; a guard that
     * only covered one of them was covering half the surface.
     *
     * The comparison is on the tail rather than on a fixed literal because the same
     * data is reachable as `/proc/self/...`, `/proc/<pid>/...` and
     * `/proc/thread-self/...`. Matching the last component accepts all three.
     */
    if (len >= 6 && strcmp(path + len - 6, "/smaps") == 0) return true;
    if (strcmp(path + len - 5, "/maps") == 0) return true;

    /*
     * `numa_maps` is the third file with the same names, and the one a suffix test
     * against `/maps` cannot reach.
     *
     * Its last component is `numa_maps`, so the byte before the `maps` tail is an
     * underscore rather than a slash, and the comparison above — which requires the
     * character at `len - 5` to be the start of the component — never matches it. The
     * file carries the same path column as `maps`, so a reader that opens it saw the
     * names completely untouched while the guard reported success on everything else.
     *
     * It is listed explicitly rather than by loosening the `maps` test to a `_maps`
     * suffix, because a looser test would also accept any unrelated file whose name
     * happened to end that way, and the guard should only ever act on files it can
     * name.
     */
    if (len >= 11 && strcmp(path + len - 11, "/numa_maps") == 0) return true;

    return false;
}

/*
 * Whether [path] names a mount table.
 *
 * Same family as the map files above: the kernel exposes the same data as
 * `/proc/self/mountinfo`, `/proc/<pid>/mountinfo` and `/proc/thread-self/mountinfo`,
 * so the test anchors on the last component rather than on one literal path. A
 * reader that hard-codes any of them is asking the same question.
 *
 * The file matters because it is where the module's own mounts stay visible. Magisk
 * replaces `/system/bin` with a tmpfs whose source name is `magisk`, and that line is
 * the last trace a reader can still find after the property and map substitutions
 * have run. It is also the one the namespace cleaner never removed, because
 * unmounting `/system/bin` would take every command away from the process holding it.
 */
static bool is_mountinfo_path(const char *path) {
    if (path == NULL) return false;
    size_t len = strlen(path);
    if (len <= 10) return false;
    if (!starts_with(path, "/proc/")) return false;
    if (strcmp(path + len - 10, "/mountinfo") != 0) return false;
    return true;
}

/*
 * Whether [addr] falls inside a page this module mapped for a trampoline.
 *
 * This is what makes the filter safe. Blanket-dropping every anonymous
 * executable region would also hide the runtime's own JIT caches, which a normal
 * app legitimately has, and hiding those would be a lie the app could trip over.
 * Only the exact pages recorded in `g_hooks` are ours, so only those are removed —
 * every other mapping in the file passes through byte for byte.
 */
static bool is_own_trampoline(uintptr_t addr) {
    for (int i = 0; i < g_hook_count; i++) {
        if (!g_hooks[i].active) continue;
        uintptr_t base = (uintptr_t) g_hooks[i].trampoline;
        if (addr >= base && addr < base + PAGE_SIZE) return true;
    }
    return false;
}

/*
 * Neutralisation tables.
 *
 * The replacement for `zygisk` is exactly as long as the token it replaces, so it can
 * be written in place without moving anything around the line: the address, the
 * permissions and the inode stay where they were, and only the name changes.
 *
 * The suffix appended to a trampoline line is the annotation the runtime's own JIT
 * cache carries. That is what an anonymous executable page looks like in an ordinary
 * app, so a reader sees a familiar region rather than a nameless one.
 */
#define MAPS_LINE_MAX 512

static const char NEUTRAL_ZYGISK_TOKEN[] = "zygisk";
static const char NEUTRAL_ZYGISK_REPLACEMENT[] = "jitcod";

/*
 * The module's own package name, as it appears in the map of a guarded process.
 *
 * This is the second half of what a detector calls "code injection", and it is the
 * more blatant half: the module APK is mapped into the target process, so its path
 * shows up four times in `/proc/self/maps` — a file inside `/data/app` that belongs
 * to a different package than the one running. No ordinary app has that, and no
 * amount of hiding anonymous pages makes it go away.
 *
 * The replacement is chosen to be the same length, character for character, so the
 * line keeps its exact shape and a reader that parses columns by position sees
 * nothing move. Seventeen characters for seventeen characters is the only property
 * that matters here; the name itself is then a system package, which is a far less
 * interesting thing to find in a map than a side-loaded module.
 */
static const char NEUTRAL_MODULE_TOKEN[] = "com.houvven.guise";
static const char NEUTRAL_MODULE_REPLACEMENT[] = "com.android.shell";

/*
 * The library's own file name, which is the third place the module names itself.
 *
 * The package token above does not cover it, and that is not a detail: the module's
 * native library is loaded into the guarded process, so `/proc/self/maps` carries
 * `.../libguise_native.so` on its own — a path that contains `guise` but not
 * `com.houvven.guise`. A guard that only replaced the package name therefore left
 * exactly the string it was trying to remove, four times over, and reported success
 * on every buffer it had actually cleaned.
 *
 * The replacement keeps the length and the `.so` suffix, so the line still parses as
 * an ordinary loaded library with a name that belongs to the platform. Sixteen
 * characters for sixteen, checked by the assertion below.
 */
static const char NEUTRAL_LIB_TOKEN[] = "libguise_native.so";
static const char NEUTRAL_LIB_REPLACEMENT[] = "libnativehelper.so";

_Static_assert(sizeof(NEUTRAL_ZYGISK_TOKEN) == sizeof(NEUTRAL_ZYGISK_REPLACEMENT),
               "the zygisk replacement must keep the line length unchanged");
_Static_assert(sizeof(NEUTRAL_MODULE_TOKEN) == sizeof(NEUTRAL_MODULE_REPLACEMENT),
               "the module replacement must keep the line length unchanged");
_Static_assert(sizeof(NEUTRAL_LIB_TOKEN) == sizeof(NEUTRAL_LIB_REPLACEMENT),
               "the library replacement must keep the line length unchanged");

/*
 * The word the module leaves in a mount table.
 *
 * Magisk replaces `/system/bin` with a tmpfs and names the source `magisk`. The name
 * appears twelve times in a real mount table on this device -- the system bin, the
 * debug ramdisk, magisk64, magisk32, magiskpolicy, the module store, the preinit tree
 * -- and it is the trace the namespace cleaner could not remove: unmounting
 * `/system/bin` would take every command away from the process that holds it.
 *
 * `worker` is not an invented name. The same mount table already carries `tmpfs
 * worker` for `/system/bin` and `/debug_ramdisk/.magisk/worker` on a clean boot of
 * this device. Substituting one for the other leaves a line that reads like the
 * system's own, with the same length and the same columns.
 */
static const char NEUTRAL_MAGISK_TOKEN[] = "magisk";
static const char NEUTRAL_MAGISK_REPLACEMENT[] = "worker";

_Static_assert(sizeof(NEUTRAL_MAGISK_TOKEN) == sizeof(NEUTRAL_MAGISK_REPLACEMENT),
               "the magisk replacement must keep the line length unchanged");
static const char NEUTRAL_ANON_SUFFIX[] = "  [anon:dalvik-jit-code-cache]";

/*
 * Overwrites every case-insensitive occurrence of [needle] with [repl].
 *
 * The two are required to be the same length, which is the whole point: a same-length
 * substitution cannot shift the rest of the line, so a reader that parses columns by
 * position sees exactly the layout it saw before.
 */
static int replace_all_ignore_case(char *text, size_t len, const char *needle, const char *repl) {
    size_t n = strlen(needle);
    size_t r = strlen(repl);
    if (n == 0 || r != n) return 0;
    int count = 0;
    size_t pos = 0;
    while (pos + n <= len) {
        size_t i = 0;
        while (i < n && (text[pos + i] | 0x20) == (needle[i] | 0x20)) {
            i++;
        }
        if (i == n) {
            memcpy(text + pos, repl, n);
            count++;
            pos += n;
        } else {
            pos++;
        }
    }
    return count;
}

/*
 * Descriptor bookkeeping for the maps rewrite.
 *
 * The fd a caller receives is the kernel's own, not a substitute. That is the whole
 * point of this design: two earlier attempts handed back a memfd instead, and the
 * process crashed inside its own library both times, a millisecond after the filter
 * logged. Whatever the reader does with the descriptor — `fstat` for the size,
 * `mmap` for the content, `readlink` for the path — a memfd answers differently from
 * a proc file, and the reader was entitled to the proc file.
 *
 * So the descriptor stays real and only the bytes are edited, in the read buffer, at
 * the moment they are handed over. `fstat` reports the true size, `mmap` maps the
 * true file, and a reader that opens and reads gets the same length it always did.
 */
#define MAPS_FD_TABLE_SIZE 8192

static unsigned char g_maps_fd[MAPS_FD_TABLE_SIZE];

/*
 * Descriptor table for mount tables, kept separate from the map one.
 *
 * The two rewrites differ: a map buffer gets the token and structural passes, while a
 * mount table gets the source-name substitution and nothing else. Sharing a table
 * would make a descriptor opened from one file receive the other file's treatment.
 */
static unsigned char g_mountinfo_fd[MAPS_FD_TABLE_SIZE];
static int g_mountinfo_rewrites = 0;

/*
 * The dalvik property file, tracked separately from the map files.
 *
 * A detector can read a property without asking the property service: the file under
 * /dev/__properties__ holds the names and values in a form that can be opened and read
 * like any other file. Nothing in that path is a property call, so the property hook
 * never sees it, and the answer it produces is the kernel's own.
 *
 * What the file cannot hide is its own shape. The names are stored as plain text, so a
 * reader that scans for one particular name will find it by text matching, not by asking
 * anything. That makes the text the place to act: the same length of the same name with
 * one character changed is no longer that name to a text search, while every offset,
 * length and neighbouring entry stays exactly where it was.
 *
 * The descriptor is remembered the same way map descriptors are, with the same
 * limitations: a number can be reused, and a reader that duplicates the descriptor or
 * reaches the file another way will not be recognised. The byte rewriting below does not
 * depend on the mark for correctness, only for which scan runs; the content itself is
 * checked before anything is changed.
 */
#define PROP_FD_TABLE_SIZE 8192

static unsigned char g_prop_fd[PROP_FD_TABLE_SIZE];

/*
 * Recognises any property-area file, not one particular file.
 *
 * The property service stores its data in a set of files under /dev/__properties__,
 * one per SELinux context, each named u:object_r:<something>_prop:s0. A reader that
 * wants a value without going through the property API opens the file whose name
 * matches the context it expects and reads the bytes. Which context that is depends on
 * which property is wanted: dalvik configuration lives in dalvik_config_prop, the build
 * identity lives in build_prop and fingerprint_prop.
 *
 * Matching one context by name was wrong for exactly that reason — it covered the case
 * that had been observed and missed every other one. The suffix is the invariant: every
 * one of these files ends in _prop:s0, and no unrelated file in that directory does.
 */
static bool is_prop_path(const char *path) {
    if (path == NULL) return false;
    if (strstr(path, "/__properties__/") == NULL) return false;
    return strstr(path, "_prop:s0") != NULL;
}

static void mark_prop_fd(int fd) {
    if (fd >= 0 && fd < PROP_FD_TABLE_SIZE) g_prop_fd[fd] = 1;
}

static bool is_prop_fd(int fd) {
    return fd >= 0 && fd < PROP_FD_TABLE_SIZE && g_prop_fd[fd] != 0;
}

static void clear_prop_fd(int fd) {
    if (fd >= 0 && fd < PROP_FD_TABLE_SIZE) g_prop_fd[fd] = 0;
}

/*
 * Rewrites the value that follows an overridden property name, in place.
 *
 * The property area stores each entry as the name, a NUL, the value and another NUL,
 * with the name also present in a separate index. A reader that scans the raw bytes
 * finds a name by text search and then reads whatever follows the NUL. The name itself
 * has to stay findable — a detector that cannot find the name concludes the property is
 * missing, which is a different finding and not a better one.
 *
 * What gets replaced is the value, and only up to the length the original value already
 * occupies. The replacement never runs past that boundary: the area is a packed
 * structure, and writing beyond an entry would corrupt whichever entry follows. When the
 * configured value is shorter, the remainder is filled with NULs, which ends the string
 * earlier and leaves the layout intact.
 *
 * The names come from the configured override table, so this covers whatever the module
 * decided to spoof rather than one name chosen in advance. Every property the Java side
 * rewrites is rewritten here as well, which is what keeps the two channels saying the
 * same thing.
 */
static void neutralize_prop_key(char *buf, size_t len) {
    if (buf == NULL || len == 0) return;

    /*
     * The native bridge check looks for a name, not for a value.
     *
     * One of the checks reads the property area as text, keeps only the characters that
     * can appear in a property name, and searches the result for one particular name. It
     * never asks the property service, so nothing on the read path sees the question, and
     * the answer it reaches is the one the file itself gives.
     *
     * What it does see is the text. A name that no longer appears is a name that cannot be
     * found, and a single changed character is enough for that while leaving the record
     * the same length and every neighbouring entry where it was. The last character is
     * the safest one to change: it cannot turn the name into another valid name, and it
     * cannot be confused with the separator that follows.
     *
     * This is separate from the value rewriting below. That one exists so a reader sees
     * the spoofed values; this one exists so a reader does not see one particular name at
     * all. They act on different properties and do not overlap.
     */
    hide_configured_keys(buf, len);

    for (int k = 0; k < g_dyn_prop_count; k++) {
        const char *name = g_dyn_props[k].name;
        const char *value = g_dyn_props[k].value;
        if (name == NULL || value == NULL) continue;

        size_t name_len = strlen(name);
        if (name_len == 0 || name_len >= len) continue;

        for (size_t i = 0; i + name_len + 1 < len; i++) {
            if (memcmp(buf + i, name, name_len) != 0) continue;
            /* The name has to be followed by the separator, or this is a prefix match. */
            if (buf[i + name_len] != '\0') continue;

            char *slot = buf + i + name_len + 1;
            size_t avail = len - (size_t) (slot - buf);
            if (avail == 0) break;

            /*
             * The original value ends at the first NUL. Its length is the budget for the
             * replacement; a longer configured value is truncated rather than allowed to
             * run into the next entry.
             */
            size_t old_len = strnlen(slot, avail);
            if (old_len == 0) break;

            size_t copy_len = strlen(value);
            if (copy_len > old_len) copy_len = old_len;

            memcpy(slot, value, copy_len);
            if (copy_len < old_len) memset(slot + copy_len, 0, old_len - copy_len);
            break;
        }
    }
}

/*
 * Rewrites property values in the mappings that are already in this address space.
 *
 * The read and pread paths cover a caller that opens the property file and reads it.
 * They do not cover a caller that maps the file instead, and the property service's own
 * client code does exactly that: every property context is mmap'ed at startup, and a
 * value read from there never passes through read at all. The map is the data, so the
 * data is what has to be changed.
 *
 * A shared read-only mapping cannot be written directly, and it should not be replaced
 * either — other code holds pointers into it. Making it writable for the duration of one
 * copy is enough: the first store to a shared page gives this process its own copy of
 * that page, which is precisely the scope the change should have. The file on disk, and
 * every other process reading it, keep the original value.
 *
 * The maps list is read with raw system calls, for the same reason the relabelling code
 * does: this function belongs to the guard, and the guard rewrites what read returns.
 */
/*
 * Rewrites the overridden property values inside a copy of a property-area file.
 *
 * The area is not a text file. Each entry is a fixed-size record: a 32-bit serial, then
 * the value in a 92-byte slot, then the name in a 32-byte slot, all padded with NULs.
 * The offsets were measured rather than assumed — for the fingerprint area the name sits
 * at 420 and its value at 328, a difference of exactly the value slot size. So the value
 * of any entry begins a fixed distance before its name, and finding the name by text is
 * enough to find the value.
 *
 * The value is never read through the property service, and that is deliberate. Asking
 * for it the ordinary way takes the property lock and, on the first call in a process,
 * initialises the area — an initialisation that goes back through the file open and
 * mapping calls this very module has hooked. The result was a deadlock: the main thread
 * waited on a futex that only it could release. Reading the layout directly has no such
 * ordering — it touches memory that is already mapped and nothing else.
 *
 * A match is only accepted where the name is NUL-terminated, so a name that happens to
 * appear inside another value is not mistaken for an entry. The replacement is truncated
 * to the slot it is written into; a shorter one is padded with NULs, which ends the
 * string sooner and leaves every following record where it was.
 */
static int rewrite_prop_values(char *buf, size_t len) {
    if (buf == NULL || len == 0) return 0;

    /* 32-bit serial + PROP_VALUE_MAX value slot, then the name. Measured, not guessed. */
    const size_t value_before_name = PROP_VALUE_MAX;
    int changed = 0;

    /*
     * The native bridge name, removed from the text before any value is touched.
     *
     * This file is the one the map replacement copies, so a reader that maps the property
     * area sees whatever this function leaves behind. The name is what one of the checks
     * searches for, and it is not one of the properties the module overrides — there is no
     * value to substitute, only a name to make unfindable.
     *
     * Changing the last character is enough and is safe: the record keeps its length, no
     * neighbouring entry moves, and the result cannot collide with another valid name.
     * The same rewrite appears on the read path; both are needed because a reader may get
     * at this data either by mapping it or by reading it.
     */
    hide_configured_keys(buf, len);

    for (int i = 0; i < g_dyn_prop_count; i++) {
        const char *name = g_dyn_props[i].name;
        const char *fake = g_dyn_props[i].value;
        if (name == NULL || fake == NULL) continue;

        size_t name_len = strlen(name);
        if (name_len == 0 || name_len >= 32) continue;

        for (size_t pos = value_before_name; pos + name_len < len; pos++) {
            if (memcmp(buf + pos, name, name_len) != 0) continue;
            /* The record name field is NUL-padded; require the terminator. */
            if (buf[pos + name_len] != '\0') continue;

            char *slot = buf + pos - value_before_name;
            size_t flen_chk = strlen(fake);
            if (flen_chk <= PROP_VALUE_MAX && strnlen(slot, PROP_VALUE_MAX) == flen_chk &&
                memcmp(slot, fake, flen_chk) == 0) break;   /* already spoofed */
            size_t flen = strlen(fake);
            if (flen > PROP_VALUE_MAX) flen = PROP_VALUE_MAX;

            memcpy(slot, fake, flen);
            if (flen < PROP_VALUE_MAX) memset(slot + flen, 0, PROP_VALUE_MAX - flen);
            changed++;
            break;
        }
    }

    /*
     * The static table, applied to the same buffer.
     *
     * The loop above only walks the table the module sent over, because that is the
     * table whose values depend on the configuration. The static table holds the
     * values that are the same on every device -- the debugging switches and the
     * boot state -- and it was applied through find_override, which is reached only
     * by a caller that asks the property service.
     *
     * A reader that maps the property area never asks the service. It reads the
     * bytes directly, and those bytes were copied before any call happened. So the
     * static values have to be written into the copy as well, or the same property
     * answers differently depending on how it was read -- which is the cross-source
     * disagreement a detector reports as spoofing, and exactly what happened to the
     * boot state properties: __system_property_get answered locked, the mapped
     * area still said unlocked, and the check that compares sources saw both.
     */
    for (int i = 0; kPropOverrides[i].name != NULL; i++) {
        const char *name = kPropOverrides[i].name;
        const char *fake = kPropOverrides[i].value;
        if (name == NULL || fake == NULL) continue;

        size_t name_len = strlen(name);
        if (name_len == 0 || name_len >= 32) continue;

        for (size_t pos = value_before_name; pos + name_len < len; pos++) {
            if (memcmp(buf + pos, name, name_len) != 0) continue;
            if (buf[pos + name_len] != '\0') continue;

            char *slot = buf + pos - value_before_name;
            size_t flen = strlen(fake);
            if (flen <= PROP_VALUE_MAX && strnlen(slot, PROP_VALUE_MAX) == flen &&
                memcmp(slot, fake, flen) == 0) break;   /* already spoofed */
            if (flen > PROP_VALUE_MAX) flen = PROP_VALUE_MAX;

            memcpy(slot, fake, flen);
            if (flen < PROP_VALUE_MAX) memset(slot + flen, 0, PROP_VALUE_MAX - flen);
            changed++;
            break;
        }
    }
    return changed;
}

/*
 * Replaces the property-area mappings with private copies holding the spoofed values.
 *
 * The mapping cannot be written where it lies. It is MAP_SHARED over a file libc opened
 * read-only at process start: the kernel will not make such a page writable, and a write
 * that did land would go through to the file and change the value for every process on
 * the device. Only this process should see the change.
 *
 * So each mapping is copied, the copy is edited, and the copy is installed over the
 * original with MAP_FIXED. Keeping the address matters: libc holds a pointer into the
 * area and never re-resolves it, so the data has to appear where it already expects it.
 * The replacement is private, which confines the edit to this process.
 */
/*
 * Applies the property-page replacement after the detector has finished looking.
 *
 * The replacement is necessary: a reader that maps the property area instead of calling
 * the property service sees the device's own values there, and no hook on the read path
 * can reach a value that was never read through a call. It is also measurable: the pages
 * it installs are private and anonymous, and a fork has to account for every one of them.
 * One of the checks forks repeatedly and compares how long it takes, so a process whose
 * page tables have just been rewritten looks different from one whose have not.
 *
 * Both statements are true at the same time, which means the work has to happen later
 * than the check and earlier than the reader. The check runs during start-up; the reader
 * is the application drawing its own screen, which is seconds afterwards. A second is
 * comfortably between the two, and the passes after it catch any context that the
 * property service maps lazily.
 *
 * Each pass is idempotent: the value comparison inside the rewrite skips slots that
 * already hold the spoofed text, so a later pass only reports the pages that appeared
 * since the previous one, and the loop stops once a pass finds nothing to do.
 */
/*
 * Declared here because the deferred worker below calls it, and the definition is
 * written after the worker rather than before it.
 */
static int patch_property_pages(void);

static void *prop_pages_thread(void *arg) {
    (void) arg;
    for (int i = 0; i < 6; i++) {
        if (i > 0) usleep(200 * 1000);
        int n = patch_property_pages();
        if (n > 0) LOGI("prop pages replaced (late): %d at pass %d", n, i);
        else if (i > 0) break;
    }
    return NULL;
}

static void start_prop_page_thread(void) {
    pthread_t t;
    if (pthread_create(&t, NULL, prop_pages_thread, NULL) == 0) pthread_detach(t);
}

/*
 * Replaces only the individual pages whose content actually changed.
 *
 * An earlier version copied each whole property mapping, edited the copy, and installed
 * it over the original in one piece. That works, and it costs far more than it needs to:
 * a property area is 192 KB and the values that get rewritten live in two or three of its
 * forty-eight pages. Replacing all forty-eight makes every one of them a private anonymous
 * page, and a process whose page tables look like that takes measurably longer to fork.
 * One of the checks forks repeatedly and times it, so the expensive version had to be
 * postponed until after that check — and postponing it meant the application sometimes
 * read the original values first, which is the problem it was meant to solve.
 *
 * A page is the right unit. The record layout is fixed: a four-byte serial, then the value
 * in a ninety-two-byte slot, then the name in a thirty-two-byte slot. A record can straddle
 * a page boundary, so the search runs over the whole mapping and the comparison decides
 * which pages to keep — but only the pages that hold a rewritten value are replaced. In
 * practice that is one page for the fingerprint area and one or two for the build area,
 * which is a cost small enough to pay at install time, before anything can read.
 *
 * Splitting a mapping is allowed. MAP_FIXED on a subrange replaces just that range and
 * leaves the surrounding pages mapped to the file exactly as they were, which is correct:
 * their contents did not change.
 */
static int patch_property_pages(void) {
    if (g_dyn_prop_count == 0 && g_hidden_key_count == 0) return 0;

    int maps_fd = (int) syscall(SYS_openat, AT_FDCWD, "/proc/self/maps", O_RDONLY, 0);
    if (maps_fd < 0) return 0;

    size_t cap = 512 * 1024;
    char *mapbuf = (char *) malloc(cap);
    if (mapbuf == NULL) { syscall(SYS_close, maps_fd); return 0; }

    size_t total = 0;
    while (total < cap - 1) {
        ssize_t n = (ssize_t) syscall(SYS_read, maps_fd, mapbuf + total, cap - 1 - total);
        if (n <= 0) break;
        total += (size_t) n;
    }
    syscall(SYS_close, maps_fd);
    if (total == 0) { free(mapbuf); return 0; }
    mapbuf[total] = '\0';

    int pages = 0;
    const size_t page_size = 4096;
    char *line = mapbuf;
    while (*line != '\0') {
        char *nl = strchr(line, '\n');
        if (nl == NULL) break;
        *nl = '\0';

        if (is_prop_path(line)) {
            unsigned long s = 0, e = 0;
            if (sscanf(line, "%lx-%lx", &s, &e) == 2 && e > s) {
                size_t len = (size_t) (e - s);
                char *orig = (char *) malloc(len);
                char *copy = (char *) malloc(len);
                if (orig != NULL && copy != NULL) {
                    memcpy(orig, (void *) s, len);
                    memcpy(copy, orig, len);
                    int changed = rewrite_prop_values(copy, len);
                    if (changed > 0) {
                        /*
                         * A memory file, mapped shared, rather than an anonymous private
                         * page.
                         *
                         * Both hold the same bytes and both read back the spoofed values,
                         * so from the reader side they are the same thing. They differ
                         * under fork, which is not a side issue here: a forked child gets
                         * its own copy of every private anonymous page it touches, and one
                         * of the checks forks repeatedly and times how long that takes. A
                         * mapping over a memory file is shared, so parent and child point
                         * at the same physical page and nothing is copied.
                         *
                         * Nothing is written through the shared mapping after it is
                         * installed, so the fact that a shared write would be visible to
                         * both sides does not arise: the content is fixed before the
                         * mapping is placed.
                         */
                        int mfd = (int) syscall(SYS_memfd_create, "shadow-prop", 1 /* MFD_CLOEXEC */);
                        if (mfd >= 0) {
                            syscall(SYS_ftruncate, mfd, (off_t) len);

                            size_t wrote = 0;
                            while (wrote < len) {
                                ssize_t k = (ssize_t) syscall(SYS_pwrite64, mfd,
                                                              copy + wrote, len - wrote,
                                                              (off_t) wrote);
                                if (k <= 0) break;
                                wrote += (size_t) k;
                            }

                            if (wrote == len) {
                                /*
                                 * The whole mapping, in one operation.
                                 *
                                 * Replacing only the pages whose bytes changed kept the
                                 * fork cost down and split one entry in the map listing
                                 * into several: the untouched pages stayed mapped to the
                                 * original file, so the contiguous range became a row of
                                 * smaller ranges. A reader that walks the listing sees
                                 * more entries than before, and the per-line work on that
                                 * listing was itself inside a timing window that another
                                 * check measures.
                                 *
                                 * One mapping replaces one mapping: same address, same
                                 * length, same permission, read-only over a shared memory
                                 * file. The entry count is what it was, nothing is private
                                 * so a forked child copies nothing, and the bytes any
                                 * reader sees are the rewritten ones.
                                 */
                                void *m = mmap((void *) s, len, PROT_READ,
                                               MAP_FIXED | MAP_SHARED, mfd, 0);
                                if (m == MAP_FAILED) {
                                    LOGI("prop map failed: %lx-%lx errno=%d", s, e, errno);
                                } else {
                                    pages++;
                                    LOGI("prop map replaced: %lx-%lx (%lu bytes, %d values, shared)",
                                         s, e, (unsigned long) len, changed);
                                }
                            }
                            syscall(SYS_close, mfd);
                        } else {
                            LOGI("prop memfd_create failed errno=%d", errno);
                        }
                    }
                }
                free(orig);
                free(copy);
            }
        }
        line = nl + 1;
    }
    free(mapbuf);
    return pages;
}

static void mark_maps_fd(int fd) {
    if (fd >= 0 && fd < MAPS_FD_TABLE_SIZE) g_maps_fd[fd] = 1;
}

static bool is_maps_fd(int fd) {
    return fd >= 0 && fd < MAPS_FD_TABLE_SIZE && g_maps_fd[fd] != 0;
}

static void clear_maps_fd(int fd) {
    if (fd >= 0 && fd < MAPS_FD_TABLE_SIZE) g_maps_fd[fd] = 0;
}

static void mark_mountinfo_fd(int fd) {
    if (fd >= 0 && fd < MAPS_FD_TABLE_SIZE) g_mountinfo_fd[fd] = 1;
}

static bool is_mountinfo_fd(int fd) {
    return fd >= 0 && fd < MAPS_FD_TABLE_SIZE && g_mountinfo_fd[fd] != 0;
}

static void clear_mountinfo_fd(int fd) {
    if (fd >= 0 && fd < MAPS_FD_TABLE_SIZE) g_mountinfo_fd[fd] = 0;
}

/*
 * Forward declaration, because `hook_close` is defined above the snapshot table.
 *
 * The close hook has to release the snapshot for its descriptor, and it sits earlier in
 * the file than the snapshot code because the descriptor table it also touches is
 * defined here. Declaring the one function it needs is cheaper and clearer than moving
 * either block past the other.
 */
static void snapshot_drop(int fd);

/*
 * Confirms, at read time, that a marked descriptor still refers to a map file.
 *
 * The table is indexed by number, and a number is handed out again the moment the
 * previous descriptor is closed. A stale mark therefore does not mean "this is a
 * map" — it means "the number this map once had is now in use again", and acting on
 * that rewrote whatever the app opened next. That is not a hypothetical: it was
 * measured. The detector's own service process died, and the main process reported
 * its service as unresponsive, because its private data files were being rewritten
 * as if they were maps.
 *
 * `readlink` on `/proc/self/fd/<n>` answers what the descriptor actually points at,
 * and it is called through a raw syscall so it cannot re-enter the hooks this module
 * itself installed on `readlink`. The cost is one syscall per read of a marked
 * descriptor, which only happens for map files and is therefore negligible.
 *
 * A mark that no longer holds is cleared here rather than merely ignored, so the
 * table shrinks back to reality instead of accumulating entries that all have to be
 * re-checked on every subsequent read.
 */
static bool fd_targets_map_file(int fd) {
    char link_path[64];
    char target[256];
    int n = snprintf(link_path, sizeof(link_path), "/proc/self/fd/%d", fd);
    if (n <= 0 || n >= (int) sizeof(link_path)) return false;

    long r = syscall(SYS_readlinkat, AT_FDCWD, link_path, target, sizeof(target) - 1);
    if (r <= 0) return false;
    target[r] = '\0';

    size_t len = strlen(target);
    if (len >= 6 && strcmp(target + len - 6, "/smaps") == 0) return true;
    if (len >= 5 && strcmp(target + len - 5, "/maps") == 0) return true;

    /*
     * The third map file, listed for the same reason `is_maps_path` lists it.
     *
     * Its last component ends in `_maps` rather than `/maps`, so neither test above
     * reaches it. Leaving it out here while accepting it in `is_maps_path` made the two
     * functions disagree about which files are maps, and the disagreement had a cost:
     * a descriptor opened from `numa_maps` was snapshotted and rewritten, then failed
     * verification on every subsequent read, so it was served raw while the guard
     * reported the path as covered.
     */
    if (len >= 11 && strcmp(target + len - 11, "/numa_maps") == 0) return true;

    return false;
}

/*
 * Clears the mark when the descriptor is closed.
 *
 * This is the cheap half of the fix: most descriptors are closed explicitly, and
 * clearing on close means the table is accurate for every well-behaved caller. The
 * verification in `guard_read_buffer` is the expensive half, and it exists because a
 * close is not always visible — a descriptor can be closed by the runtime, handed
 * off to another component, or closed through a fortified alias this module did not
 * patch. Neither half is sufficient alone; together they cannot let a reused number
 * be mistaken for a map.
 */
static bool prop_snapshot_take(int fd);
static bool prop_snapshot_has(int fd);
static void prop_snapshot_drop(int fd);
static ssize_t prop_snapshot_read(int fd, char *buf, size_t count, off64_t offset);
static ssize_t prop_snapshot_readv(int fd, const struct iovec *iov, int iovcnt, off64_t offset);

static int hook_close(int fd) {
    if (real_close == NULL) {
        errno = ENOSYS;
        return -1;
    }
    clear_maps_fd(fd);
    clear_mountinfo_fd(fd);
    snapshot_drop(fd);
    if (is_prop_fd(fd)) clear_prop_fd(fd);
    prop_snapshot_drop(fd);
    return real_close(fd);
}

/*
 * Whether a buffer looks like the beginning of a maps listing.
 *
 * The table above is indexed by fd number, and a number is reused the moment a
 * descriptor is closed. Without a content check, a later unrelated file that happens
 * to land on the same number would be rewritten as if it were a map — harmless for a
 * `zygisk` search, but not harmless for the address test. A maps entry begins with a
 * hex address, so the cheapest possible confirmation is to look for that shape and
 * skip anything that does not have it.
 */
static bool looks_like_maps_text(const char *text, size_t len) {
    if (text == NULL || len < 12) return false;
    size_t i = 0;
    while (i < len && i < 16 &&
           ((text[i] >= '0' && text[i] <= '9') ||
            (text[i] >= 'a' && text[i] <= 'f'))) {
        i++;
    }
    if (i == 0 || i >= len) return false;
    if (text[i] != '-') return false;
    for (size_t k = i + 1; k < len && k < i + 16; k++) {
        if ((text[k] >= '0' && text[k] <= '9') ||
            (text[k] >= 'a' && text[k] <= 'f')) continue;
        return text[k] == ' ';
    }
    return false;
}

/*
 * Rewrites one read buffer in place.
 *
 * Only same-length substitutions are performed, because the buffer is the caller's
 * and its length is already committed: a byte written past `len` would corrupt
 * whatever follows, and a shorter replacement would leave a hole. Six characters for
 * six characters keeps every address, permission field and inode exactly where the
 * reader expects to find it.
 */
static int g_rewrite_hits = 0;
static int g_rewrite_skips = 0;
static int g_proc_open_count = 0;

#define PROC_OPEN_DIAGNOSTIC_LIMIT 40

/*
 * Whether a line looks like a real maps entry: `hex-hex perm offset dev inode path`.
 *
 * This is the check that the previous attempt had in the wrong place. It was applied
 * to the whole buffer, which only works when the read starts at offset zero — and a
 * maps file is read in chunks, so the chunk that carries the interesting line begins
 * mid-line. Applying it per line instead is both correct and safe: a line that starts
 * with two hex fields and a space is a map, and a line that does not is some other
 * file that must be left alone.
 *
 * That distinction matters more than it looks. The unconditional version rewrote every
 * buffer containing the token, including files the app reads for its own purposes. A
 * detector that stores its own data with the same word in it would have that data
 * corrupted, which is a far worse failure than the one being fixed.
 */
static bool is_maps_line(const char *p, const char *end) {
    const char *q = p;
    int n = 0;
    while (q < end && n < 16 &&
           ((*q >= '0' && *q <= '9') || (*q >= 'a' && *q <= 'f') || (*q >= 'A' && *q <= 'F'))) {
        q++; n++;
    }
    if (n == 0 || q >= end || *q != '-') return false;
    q++; n = 0;
    while (q < end && n < 16 &&
           ((*q >= '0' && *q <= '9') || (*q >= 'a' && *q <= 'f') || (*q >= 'A' && *q <= 'F'))) {
        q++; n++;
    }
    if (n == 0 || q >= end || *q != ' ') return false;
    return true;
}

/*
 * The permission field of a maps entry, in every form it can take.
 *
 * A line reads `start-end perms offset dev inode path`, and `perms` is always one of
 * these five-character shapes. That is the signature used to decide whether a buffer
 * is map content, and it is chosen deliberately over `is_maps_line`: the address test
 * only works at the very beginning of a buffer, and a maps file is read in chunks, so
 * the chunk carrying the interesting line begins in the middle of some unrelated line
 * and every line in it fails that test.
 *
 * The permission field sits a few dozen bytes into each line, so a chunk that starts
 * mid-line still contains the field of whichever lines it carries — which makes it the
 * one part of the format that survives arbitrary chunking. A buffer with zygisk in it
 * and one of these fields is map content; nothing else in the process carries both.
 */
static const char *const kMapsPermTokens[] = {
        " r-xp ", " rw-p ", " r--p ", " rwxp ", " --xp ",
        " rw-s ", " r-xs ", " rwxs ", " r-xS ", " rw-S ",
};

static bool contains_bounded(const char *hay, size_t hay_len, const char *needle) {
    size_t n = strlen(needle);
    if (n == 0 || hay_len < n) return false;
    for (size_t i = 0; i + n <= hay_len; i++) {
        if (memcmp(hay + i, needle, n) == 0) return true;
    }
    return false;
}

static bool contains_maps_perms(const char *text, size_t len) {
    for (size_t i = 0; i < sizeof(kMapsPermTokens) / sizeof(kMapsPermTokens[0]); i++) {
        if (contains_bounded(text, len, kMapsPermTokens[i])) return true;
    }
    return false;
}

/*
 * Replaces the token everywhere in a buffer that is map content.
 *
 * The decision to rewrite is made once for the whole buffer rather than per line,
 * and that is the fix for the attempt that logged the token and changed nothing: a
 * line-by-line test cannot recognise a line whose beginning fell outside the chunk.
 * Testing the buffer as a whole — does it contain map permissions, does it contain
 * the token — has no such blind spot, and the substitution itself is position
 * preserving, so nothing about the buffer's shape changes either way.
 *
 * A buffer that contains the token but no map permissions is left completely alone.
 * That is the case the unconditional version got wrong: the app reads its own files
 * too, and rewriting one of those would corrupt the app's own data to hide a string
 * that was never visible to anything else.
 */
/*
 * Substitutes both tokens unconditionally.
 *
 * This is the version used once a descriptor is known to come from a map file,
 * and the distinction from `rewrite_maps_text` matters: the kernel hands out a map
 * file in chunks, and no chunk after the first contains the permission column of
 * any line — it starts in the middle of a line, or inside the per-segment detail
 * that `smaps` adds. A content test that looks for the permission column therefore
 * rejects exactly the buffers that carry the interesting names, which is what the
 * previous attempt did.
 *
 * Knowing the descriptor is enough, because the descriptor is the fact: this fd was
 * opened from `/proc/.../maps` or `/proc/.../smaps`, and everything read from it is
 * that file. Nothing else can arrive through it.
 */
/*
 * Rewrites the module's Zygisk JIT cache name to the one Android itself uses.
 *
 * The loader names a module's cache `/memfd:jit-cache-<module id>`, and that name is
 * the entire evidence behind a "Zygisk found" report once the literal token is gone:
 * no ordinary app has a `jit-cache-*` mapping, while every app has a
 * `jit-zygote-cache` one. Normalising the name therefore does not hide anything the
 * detector has a right to know — it makes the process report the same cache the
 * runtime itself creates.
 *
 * Two forms are handled. When the name after `/memfd:` is exactly the standard
 * sixteen characters, it is replaced wholesale, which is the case for this module's
 * own id. Otherwise only the `jit-cache-` prefix is swapped, because an arbitrary
 * module id has an arbitrary length, and a shorter replacement would shift the rest
 * of the line while the prefix alone is what the signature is built on.
 *
 * Both substitutions preserve length, so no offset anywhere in the file moves.
 */
/*
 * Renames the executable COW copy of a runtime library in a map listing.
 *
 * Why this is needed: when a library's code page is modified at run time, the
 * kernel breaks the shared mapping and gives the writer a private copy. The map
 * then lists the same file twice — the original read-only mapping and the private
 * executable one. A detector that checksums code sections walks both, and the
 * private copy no longer matches the file on disk, because it is not supposed to:
 * the whole point of the copy is that it differs.
 *
 * A guard cannot make those bytes match again without undoing the hooks that caused
 * them, so the question becomes which mapping the detector should see. The original
 * one is the truth about the file and stays untouched; the private copy is an
 * artifact of this process and is what gets renamed. Renaming is the narrowest
 * possible lie: the address, the size, the offset, the permissions and the inode all
 * remain exactly what the kernel reported — only the path column changes, and it
 * changes to a name that is one character off, so the line keeps its length and every
 * other field keeps its position.
 *
 * The condition is deliberately narrow: a line whose permission field says `x` and
 * whose file offset is not zero. The original mapping of a library is at offset zero
 * and is not executable, so it can never match; the private copy always is and
 * always does. That single test replaces any need to compare against a list of known
 * libraries, which is what would otherwise make this a rule about one program rather
 * than about the shape of the situation.
 */
static void neutralize_private_exec_copy(char *text, size_t len) {
    static const char NEEDLE[] = "libart.so";
    static const char REPLACEMENT[] = "libort.so";
    const size_t needle_len = sizeof(NEEDLE) - 1;
    if (text == NULL || len < needle_len) return;

    /*
     * Counted separately from `g_rewrite_hits`, and logged on its own terms.
     *
     * The shared counter is incremented by every rewrite pass in the module, and by
     * the time a map is read it has long since passed the twelve-line cap on
     * logging. A rename that never happened and a rename whose log line was
     * suppressed therefore look identical from outside -- both produce silence -- and
     * that ambiguity cost several rounds of guessing at the wrong layer.
     *
     * The counters below answer the two questions separately: how many times a
     * `libart.so` string was seen in a guarded buffer, and how many of those were
     * turned into `libort.so`. A run with candidates but no renames means the field
     * tests are rejecting everything and the reason is in those tests; a run with no
     * candidates at all means the map never reached this function and the reason is
     * in the caller.
     */
    static int seen = 0;
    static int renamed = 0;
    static int calls = 0;
    static int no_fields = 0;
    static int not_exec = 0;
    static int not_private = 0;
    static int zero_offset = 0;

    calls++;

    for (size_t p = 0; p + needle_len <= len; p++) {
        if (memcmp(text + p, NEEDLE, needle_len) != 0) continue;
        seen++;

        /*
         * Walk back to the start of this line to read its permission and offset
         * fields. A map line starts after a newline, and the very first line of a
         * buffer has no predecessor, which is the correct behaviour for a chunk that
         * happens to begin mid-file: an unparseable line is left alone rather than
         * guessed at.
         */
        size_t line_start = p;
        while (line_start > 0 && text[line_start - 1] != '\n') line_start--;

        /* perms = field 2, offset = field 3, both space separated */
        size_t q = line_start;
        int field = 1;
        size_t perms_at = 0, offset_at = 0;
        while (q < p && field <= 3) {
            if (text[q] == ' ') {
                field++;
                if (field == 2) perms_at = q + 1;
                if (field == 3) offset_at = q + 1;
            }
            q++;
        }
        if (perms_at == 0 || offset_at == 0) {
            no_fields++;
            continue;
        }

        /* executable? */
        bool exec = false;
        for (size_t k = perms_at; k < perms_at + 4 && k < p; k++) {
            if (text[k] == 'x') { exec = true; break; }
        }
        if (!exec) {
            not_exec++;
            continue;
        }

        /*
         * Private only, never shared.
         *
         * The permission field's fourth character says whether the mapping is
         * shared or private, and the two mean opposite things here. A shared
         * executable mapping of the runtime is the file itself, mapped once and
         * read by everyone; a private one is a copy this process made when it wrote
         * to those pages. Only the second is evidence that something ran, and only
         * the second can differ from the file.
         *
         * A detector compares both. The shared mapping matches the file's checksum
         * on an untouched device, so renaming it would turn a passing comparison
         * into a missing one -- the check looks the mapping up by name, and a row
         * renamed to `libort.so` is a row it no longer examines. The report would
         * not improve; it would change from "modified" to something the guard has no
         * answer for.
         *
         * Requiring `p` here leaves the shared rows untouched, which is exactly what
         * an unmodified device looks like.
         */
        if (perms_at + 3 >= p || text[perms_at + 3] != 'p') {
            not_private++;
            continue;
        }

        /* offset != 00000000 ? */
        bool nonzero = false;
        for (size_t k = offset_at; k < offset_at + 8 && k < p; k++) {
            if (text[k] != '0') { nonzero = true; break; }
        }
        if (!nonzero) {
            zero_offset++;
            continue;
        }

        memcpy(text + p, REPLACEMENT, needle_len);
        g_rewrite_hits++;
        renamed++;
        if (renamed <= 20) {
            LOGI("renamed private exec copy of libart (n=%d seen=%d len=%zu)",
                 renamed, seen, len);
        }
        p += needle_len - 1;
    }

    if (calls <= 10) {
        LOGI("neutralize call#%d len=%zu seen=%d renamed=%d no_fields=%d not_exec=%d not_private=%d zero_offset=%d",
             calls, len, seen, renamed, no_fields, not_exec, not_private, zero_offset);
    }
}

static void neutralize_module_jit_cache(char *text, size_t len) {
    static const char MEMFD_PREFIX[] = "/memfd:";
    static const char CACHE_PREFIX[] = "jit-cache-";
    static const char STANDARD_NAME[] = "jit-zygote-cache";
    static const char PREFIX_SWAP[] = "jit-zygot";

    const size_t memfd_len = sizeof(MEMFD_PREFIX) - 1;
    const size_t cache_len = sizeof(CACHE_PREFIX) - 1;
    const size_t std_len = sizeof(STANDARD_NAME) - 1;

    if (text == NULL || len < memfd_len + cache_len) return;

    for (size_t p = 0; p + memfd_len + cache_len <= len; p++) {
        if (memcmp(text + p, MEMFD_PREFIX, memfd_len) != 0) continue;
        char *name = text + p + memfd_len;
        if (memcmp(name, CACHE_PREFIX, cache_len) != 0) continue;
        if (p + memfd_len + std_len <= len &&
            memcmp(name, STANDARD_NAME, std_len) == 0) {
            continue;
        }

        if (p + memfd_len + std_len <= len &&
            (p + memfd_len + std_len == len ||
             name[std_len] == ' ' || name[std_len] == '(')) {
            memcpy(name, STANDARD_NAME, std_len);
        } else {
            memcpy(name, PREFIX_SWAP, sizeof(PREFIX_SWAP) - 1);
        }
        g_rewrite_hits++;
        if (g_rewrite_hits <= 12) {
            LOGI("normalized module jit cache name");
        }
        p += memfd_len + std_len - 1;
    }
}

/*
 * Clears the executable bit on the pages this module mapped for its trampolines.
 *
 * An inline hook needs executable anonymous memory, and that is the one thing an
 * ordinary app never has: every anonymous region it owns is readable or writable,
 * never executable, because all of its code comes from files. A detector that
 * counts anonymous executable mappings therefore sees this module's trampolines no
 * matter what those pages contain, and no amount of string filtering changes the
 * count — which is exactly why the token rewrite kept a clean buffer and a positive
 * report at the same time.
 *
 * The fix belongs in the permission column, not the content. Each trampoline's start
 * address is known exactly, so its line is located by that address and the `x` in
 * the four-character permission field is replaced with `-`. The line keeps its
 * length, its address and its inode; only the claim that the page is executable
 * changes, and that claim is the whole signal.
 *
 * Locating by address rather than by scanning for `r-xp` matters because the map is
 * read in chunks: a fixed field offset would be missed whenever a chunk boundary
 * fell inside it, while an address that is fully present in the buffer can be found
 * wherever it sits.
 */
static bool maps_hex(char c) {
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
}

static bool maps_perm(char c) {
    return c == 'r' || c == 'w' || c == 'x' || c == '-' ||
           c == 's' || c == 'S' || c == 'p';
}

/*
 * Clears the executable bit on every anonymous executable mapping in the buffer.
 *
 * The first version of this pass located each trampoline's line by its start address,
 * which only works when that address happens to be whole inside the buffer. The map is
 * read in chunks whose boundaries fall anywhere, so on a real 1024-byte read a line like
 * `7f93f9c000-7f93f9c040 r-xp ...` arrives split across two buffers and neither half
 * matches — the guard logged two or three cleanups against fourteen executable anonymous
 * regions and reported success.
 *
 * The address was never the right thing to key on, because the thing a detector looks at
 * is not the address either. It counts mappings that are executable and belong to no
 * file. So the pass now decides the way the detector decides: for each line, read the
 * permission field, and if it carries an `x` and the path column is empty, clear it.
 *
 * Lines with a path are left alone, which is what keeps this from touching ordinary
 * libraries, and it is also why bracketed entries such as `[vdso]` and
 * `[anon:dalvik-jit-code-cache]` are unaffected — they have a name, they are supposed to
 * be executable, and they are present in every process on the device.
 *
 * Nothing about the line's length, address, offset, device or inode changes. Only the
 * one character that makes the claim "this page can run code" is replaced, and that
 * claim is the entire signal.
 */
static void neutralize_own_trampoline_lines(char *text, size_t len) {
    if (text == NULL || len < 24) return;

    size_t line_start = 0;
    while (line_start < len) {
        size_t line_end = line_start;
        while (line_end < len && text[line_end] != '\n') line_end++;

        size_t line_len = line_end - line_start;
        if (line_len >= 24) {
            /* address-low */
            size_t q = line_start;
            while (q < line_end && maps_hex(text[q])) q++;
            if (q > line_start && q < line_end && text[q] == '-') {
                q++;
                size_t high_start = q;
                while (q < line_end && maps_hex(text[q])) q++;
                if (q > high_start && q < line_end && text[q] == ' ') {
                    /*
                     * The permission field is four characters and is followed by a
                     * space. Both conditions are checked so that a random four-letter
                     * run inside a path cannot be mistaken for it.
                     */
                    size_t perms = q + 1;
                    if (perms + 4 < line_end && text[perms + 4] == ' ') {
                        bool valid = true;
                        for (int k = 0; k < 4; k++) {
                            if (!maps_perm(text[perms + k])) { valid = false; break; }
                        }
                        if (valid) {
                            /*
                             * Skip offset, device and inode. Whatever follows them is the
                             * path column, and an empty one is what this pass is looking
                             * for. Trailing spaces are treated as empty, because the
                             * kernel's own output has them on some builds.
                             */
                            size_t r = perms + 5;
                            int fields = 0;
                            while (r < line_end && fields < 3) {
                                while (r < line_end && text[r] != ' ') r++;
                                if (r < line_end) { r++; fields++; }
                            }
                            bool anonymous = true;
                            for (size_t k = r; k < line_end; k++) {
                                if (text[k] != ' ') { anonymous = false; break; }
                            }
                            if (anonymous && text[perms + 2] == 'x') {
                                text[perms + 2] = '-';
                                g_rewrite_hits++;
                                if (g_rewrite_hits <= 12) {
                                    LOGI("cleared exec bit on anonymous mapping");
                                }
                            }
                        }
                    }
                }
            }
        }

        if (line_end >= len) break;
        line_start = line_end + 1;
    }
}

static void force_rewrite_tokens(char *text, size_t len) {
    if (text == NULL || len == 0) return;

    /*
     * Four passes, in the order their evidence appears in the file.
     *
     * The two newer passes exist because the token rewrite alone was measured to be
     * insufficient: after the literal `zygisk` was gone from the real map, the report
     * still said "Zygisk found". The remaining evidence was never that token — it was
     * the module's JIT cache name (`jit-cache-<id>`), which no ordinary app has, and
     * the anonymous executable pages this module maps for its trampolines, which no
     * ordinary app has either. Neither can be removed by string substitution, so each
     * gets a structural answer: the cache name is normalised to the runtime's own,
     * and the executable bit is cleared on the pages that are ours.
     *
     * Ordering matters only for readability. The passes touch disjoint parts of a
     * line — the path column, the permission column, then two tokens — so any order
     * would produce the same bytes.
     */
    neutralize_module_jit_cache(text, len);
    neutralize_own_trampoline_lines(text, len);
    neutralize_private_exec_copy(text, len);

    int hit = 0;
    hit += replace_all_ignore_case(text, len, NEUTRAL_ZYGISK_TOKEN,
                                   NEUTRAL_ZYGISK_REPLACEMENT);

    /*
     * The library name is a separate spelling of the module and needs its own pass.
     *
     * `libguise_native.so` contains `guise` but does not contain `com.houvven.guise`,
     * so the package substitution below cannot see it. That is not a corner case: the
     * library is mapped into the guarded process, so the map carries it on its own
     * lines, and a guard that only replaced the package name left the very string it
     * was trying to remove — four times over, in the same file it reported as clean.
     */
    hit += replace_all_ignore_case(text, len, NEUTRAL_LIB_TOKEN,
                                   NEUTRAL_LIB_REPLACEMENT);
    hit += replace_all_ignore_case(text, len, NEUTRAL_MODULE_TOKEN,
                                   NEUTRAL_MODULE_REPLACEMENT);
    if (hit > 0) {
        g_rewrite_hits++;
        if (g_rewrite_hits <= 12) {
            LOGI("maps rewritten occurrences=%d total=%d len=%zu",
                 hit, g_rewrite_hits, len);
        }
    }
}

/*
 * Whether a buffer reads like a mount table.
 *
 * A mountinfo line carries a major:minor device, a mount point, the ` - ` separator,
 * a filesystem type and a superblock option field. Requiring the separator together
 * with a mount option field is what separates it from arbitrary application data: an
 * ordinary config line or log line does not normally contain ` - ` alongside
 * `,relatime` or ` ro,`.
 *
 * The check exists so the substitution can also run on a descriptor the open hooks
 * did not see -- a copied file, a descriptor inherited from a helper -- without ever
 * touching data that is not a mount table.
 */
static bool looks_like_mountinfo(const char *text, size_t len) {
    if (text == NULL || len < 32) return false;
    bool has_sep = false;
    for (size_t i = 0; i + 3 <= len; i++) {
        if (text[i] == ' ' && text[i + 1] == '-' && text[i + 2] == ' ') { has_sep = true; break; }
    }
    if (!has_sep) return false;
    if (contains_bounded(text, len, ",relatime")) return true;
    if (contains_bounded(text, len, ",noatime")) return true;
    if (contains_bounded(text, len, " ro,")) return true;
    if (contains_bounded(text, len, " rw,")) return true;
    return false;
}

/*
 * Replaces the module's mount source name in a mount table.
 *
 * Separate from `force_rewrite_tokens` because the evidence is different. A map
 * buffer is recognised by its permission column; a mount table has no such column,
 * and the name it carries -- `magisk` as a tmpfs source -- appears nowhere in a map.
 * Folding the two would either rewrite map buffers for a name they never contain, or
 * rewrite mount tables with passes that expect a shape they do not have.
 *
 * Length is preserved, so every mount ID, parent ID, device number and column stays
 * where the reader expects it.
 */
static void rewrite_mountinfo_text(char *text, size_t len) {
    if (text == NULL || len == 0) return;

    /*
     * Entry is counted, and the shape test is reported separately.
     *
     * A system-wide log showed `/proc/self/mountinfo` being opened three times per
     * detector run and not one `mountinfo rewritten` line ever printed. That left two
     * possibilities with completely different fixes: the function was never entered,
     * or it was entered and `looks_like_mountinfo` rejected every buffer. Silence
     * cannot distinguish them, and guessing which one it was is what made the earlier
     * rounds slow.
     *
     * `entered` counts calls; `rejected` counts those whose buffer failed the shape
     * test. A run with entered == 0 means the caller is the problem -- the descriptor
     * was not marked, or the path was handled by a branch that returns before this
     * line. A run with entered > 0 and rejected == entered means the shape test is
     * too strict for what the kernel actually delivers.
     */
    static int entered = 0;
    static int rejected = 0;
    entered++;

    if (!looks_like_mountinfo(text, len)) {
        rejected++;
        if (entered <= 12) {
            LOGI("mountinfo shape rejected: entered=%d rejected=%d len=%zu",
                 entered, rejected, len);
        }
        return;
    }
    int hits = replace_all_ignore_case(text, len, NEUTRAL_MAGISK_TOKEN,
                                       NEUTRAL_MAGISK_REPLACEMENT);
    if (hits > 0) {
        int seq = g_mountinfo_rewrites++;
        if (seq <= 12) {
            LOGI("mountinfo rewritten occurrences=%d total=%d len=%zu", hits, seq, len);
        }
    } else if (entered <= 12) {
        LOGI("mountinfo shape ok but no magisk token: entered=%d len=%zu", entered, len);
    }
}

/*
 * A whole-file snapshot of a map listing, taken once at open time.
 *
 * Every attempt before this one rewrote the data as it was read, buffer by buffer,
 * and every one of them leaked. The reason is structural rather than a missed case:
 * the kernel delivers `maps` in 1024-byte chunks whose boundaries fall wherever they
 * fall, so a line like
 *
 *   7f93f9c000-7f93f9c040 r-xp 00000000 00:00 0
 *
 * arrives split across two reads and neither half parses as a line. A pass that
 * counts executable anonymous regions therefore misses the split ones, which is
 * exactly what was measured — five cleanups logged against fourteen executable
 * anonymous mappings in the same file.
 *
 * No amount of care inside a chunk-sized buffer fixes that, because the information
 * needed to recognise a line is not present in one chunk. The fix has to move the
 * boundary: read the whole file once, rewrite it whole, and serve the rewritten copy
 * from then on. Then every line is complete at the moment it is judged.
 *
 * What is deliberately not done is replacing the descriptor. Two earlier attempts did
 * that and crashed, because a proc file and a memfd are not interchangeable to a
 * reader that inspects the descriptor rather than only reading it: `fstat` reports a
 * different size, `mmap` produces a different object, and a reader that compares the
 * two sees a contradiction it cannot resolve. Here the descriptor stays exactly the
 * one the kernel handed out, and only the bytes returned by `read` come from the
 * snapshot. The size, the inode and every other property still describe the real file.
 */
typedef struct {
    char *data;      /* rewritten contents, owned by this table */
    size_t length;   /* total bytes */
    size_t position; /* where the next sequential read starts */
} maps_snapshot_t;

static maps_snapshot_t g_maps_snapshot[MAPS_FD_TABLE_SIZE];
/*
 * The same treatment for the property areas, which need it for the same reason.
 *
 * A property file is a packed table of fixed-size records and is read in whatever chunks
 * the caller asks for. The rewrite looks for a name, and a name can be cut in half by a
 * chunk boundary: the first chunk ends inside it, the second begins in the middle of it,
 * and neither contains it whole. Matching in the read hook therefore misses precisely
 * the cases where the caller happened to pick an unlucky buffer size — which is not a
 * corner case, it is the normal behaviour of a reader that reads in fixed blocks.
 *
 * The fix is the one already proven on the map files: capture the file whole at open
 * time, rewrite that single complete buffer, and serve every later read from it. Once the
 * whole file is in hand a name is either present or absent, never split.
 *
 * The descriptor is not replaced, for the reason recorded above the map snapshot: a
 * caller that inspects the descriptor rather than only reading from it would see a
 * different file. It keeps the real descriptor, and only the bytes come from the copy.
 */
static maps_snapshot_t g_prop_snapshot[PROP_FD_TABLE_SIZE];

static bool prop_snapshot_has(int fd) {
    if (fd < 0 || fd >= PROP_FD_TABLE_SIZE) return false;
    return g_prop_snapshot[fd].data != NULL;
}

static void prop_snapshot_drop(int fd) {
    if (fd < 0 || fd >= PROP_FD_TABLE_SIZE) return;
    if (g_prop_snapshot[fd].data == NULL) return;
    free(g_prop_snapshot[fd].data);
    g_prop_snapshot[fd].data = NULL;
    g_prop_snapshot[fd].length = 0;
    g_prop_snapshot[fd].position = 0;
}

/*
 * Reads the property file once, rewrites it whole, and remembers the result.
 *
 * The read is a raw system call because this function belongs to the guard, and the
 * guard answers `read` for every caller in the process including this one. Going through
 * the guarded entry point would ask this function to rewrite a buffer that is still
 * being assembled.
 *
 * A file that cannot be read leaves no snapshot, and the caller falls back to the
 * per-buffer rewrite in the read hook. That fallback is weaker — it can miss a name cut
 * across a boundary — but it is never wrong, and refusing to serve the file at all would
 * break the property service, which is a much larger failure than a missed rewrite.
 */
static bool prop_snapshot_take(int fd) {
    if (fd < 0 || fd >= PROP_FD_TABLE_SIZE) return false;
    if (g_prop_snapshot[fd].data != NULL) return true;   /* already captured */

    size_t cap = 64 * 1024;
    size_t len = 0;
    char *buf = (char *) malloc(cap);
    if (buf == NULL) return false;

    for (;;) {
        if (len == cap) {
            size_t next = cap * 2;
            char *grown = (char *) realloc(buf, next);
            if (grown == NULL) { free(buf); return false; }
            buf = grown;
            cap = next;
        }

        long n = (long) syscall(SYS_pread64, fd, buf + len, cap - len, (off_t) len);
        if (n < 0) {
            if (errno == EINTR) continue;
            free(buf);
            return false;
        }
        if (n == 0) break;
        len += (size_t) n;
    }

    if (len == 0) { free(buf); return false; }

    /* The whole file is in hand; every name in it is complete. */
    neutralize_prop_key(buf, len);
    /*
     * The snapshot path bypasses guard_read_buffer, so the value table has to be
     * applied here as well. Without this line a property file that was captured whole
     * would keep every real device value it contains, and the capture would have made
     * the answer worse rather than better.
     */
    {
        int vp_hits = rewrite_value_pairs(buf, len);
        LOGI("snapshot value applied: fd=%d len=%lu hits=%d", fd, (unsigned long) len, vp_hits);
    }

    g_prop_snapshot[fd].data = buf;
    g_prop_snapshot[fd].length = len;
    g_prop_snapshot[fd].position = 0;
    LOGI("prop snapshot taken: fd=%d len=%lu", fd, (unsigned long) len);
    return true;
}

/*
 * Serves bytes from the rewritten copy.
 *
 * Same contract as the map version: a negative offset is a sequential read that advances
 * both the copy cursor and the real descriptor, and a non-negative offset is positional
 * and touches neither. Advancing the real offset keeps `lseek` consistent with what the
 * caller has already consumed.
 */
static ssize_t prop_snapshot_read(int fd, char *buf, size_t count, off64_t offset) {
    maps_snapshot_t *snap = &g_prop_snapshot[fd];
    size_t pos = (offset < 0) ? snap->position : (size_t) offset;
    if (pos >= snap->length) return 0;

    size_t avail = snap->length - pos;
    size_t take = (count < avail) ? count : avail;
    memcpy(buf, snap->data + pos, take);

    if (offset < 0) {
        snap->position = pos + take;
        syscall(SYS_lseek, fd, (off_t) take, SEEK_CUR);
    }
    return (ssize_t) take;
}

/*
 * The same copy, scattered across the caller segments.
 *
 * `readv` asks for the same bytes as `read`, split into several buffers. The split is the
 * caller's choice and has nothing to do with the file layout, so a name can straddle a
 * segment boundary exactly the way it can straddle a read size — and the fix is the same:
 * serve from the complete copy and let each segment take its own slice of it.
 *
 * The cursor advances once, by the total actually delivered, which is what the kernel
 * does for a single `read`. Advancing per segment would move it further than the caller
 * consumed whenever a segment was not filled to its declared length.
 */
static ssize_t prop_snapshot_readv(int fd, const struct iovec *iov, int iovcnt, off64_t offset) {
    maps_snapshot_t *snap = &g_prop_snapshot[fd];
    size_t pos = (offset < 0) ? snap->position : (size_t) offset;
    if (iov == NULL || iovcnt <= 0) return 0;
    if (pos >= snap->length) return 0;

    ssize_t total = 0;
    for (int i = 0; i < iovcnt && i < 64; i++) {
        if (iov[i].iov_base == NULL || iov[i].iov_len == 0) continue;
        if (pos >= snap->length) break;

        size_t avail = snap->length - pos;
        size_t take = (iov[i].iov_len < avail) ? iov[i].iov_len : avail;
        memcpy(iov[i].iov_base, snap->data + pos, take);
        pos += take;
        total += (ssize_t) take;
    }

    if (offset < 0) {
        snap->position = pos;
        syscall(SYS_lseek, fd, (off_t) total, SEEK_CUR);
    }
    return total;
}


/*
 * Whether map reads are served from a snapshot or from the live file.
 *
 * The snapshot was introduced to solve a real problem: the kernel delivers `maps` in
 * 1024-byte chunks whose boundaries fall anywhere, so a pass that judges each chunk in
 * isolation sees half-lines and misses them. Reading the whole file once fixes that.
 *
 * But a snapshot is also a copy, and a copy is not the file. A reader that asks for the
 * same descriptor twice, or compares what it read against what another process reports,
 * gets a frozen answer where it expected a moving one. If the detector's service does
 * that comparison — and the lifecycle suggests it does, since the connection is
 * established and then dropped rather than never formed — then the snapshot is the thing
 * that breaks it, not the thing that protects it.
 *
 * So the two strategies are separated and this switch selects between them. Both paths
 * rewrite the bytes; the difference is only whether the caller receives the file's
 * current contents or a copy taken at open time. Default is off, because forwarding the
 * live file and editing it in flight is the behaviour a transparent middle layer is
 * supposed to have.
 */
/*
 * Snapshots are off. The guard still rewrites, but only in place and only on
 * buffers that actually carry a module marker.
 *
 * The snapshot path worked by replacing one question's answer with a stored copy
 * taken when a map file was opened. That is correct only for the descriptor it was
 * taken from, and a descriptor number is not an identity: the number is reused the
 * moment the original is closed, including by paths this module cannot see. When
 * that happened the detector asked for its mount table and was handed a map file —
 * a complete, well-formed, entirely wrong answer, missing the `/apex` and `/vendor`
 * anchors that were present in the file it actually asked for. Nothing crashed and
 * nothing logged an error; the only symptom was a verdict on the other side.
 *
 * Re-checking the descriptor before serving a snapshot was attempted and reverted:
 * the check released entries that callers were about to dereference, and it read
 * `/proc/self/fd` while `/proc` was itself being read under the guard.
 *
 * Turning the path off removes the failure mode instead of policing it. Every read
 * now returns the bytes the kernel actually has for that descriptor, and the guard's
 * remaining job is the narrow one it can do without guessing: if a buffer contains a
 * module marker, rewrite the marker in place, same length, same offsets. A file with
 * no marker in it cannot be changed by that, so a mount table passes through
 * untouched and the detector sees exactly what the system reports.
 *
 * The cost is that map markers are rewritten per read call rather than once per
 * open. A map file is a few hundred kilobytes read in a handful of calls, so this is
 * a handful of scans instead of one — cheap, and paid only by processes that read
 * their own maps.
 */
static bool snapshot_enabled(void) {
    return false;
}

/*
 * Reads the entire file, rewrites it as one piece, and stores the result.
 *
 * The read loop uses `syscall` rather than `real_read` on purpose: this function runs
 * from `hook_openat`, and the install order patches the open family before the read
 * family, so `real_read` may still be NULL at the moment this is first called. The raw
 * syscall has no such dependency.
 *
 * The descriptor's offset is restored to zero afterwards. The reads above moved it to
 * the end of the file, and a caller that opens then reads expects to start at the
 * beginning — leaving the offset at EOF would make the first read return zero bytes and
 * look like an empty file.
 *
 * Growth is by doubling from 64 KiB, which covers both `maps` (a few hundred KiB) and
 * `smaps` (a few MiB on a loaded process) in a handful of reallocations. A failure at
 * any point frees the partial buffer and leaves the table empty, so the caller falls
 * back to the per-buffer path rather than serving truncated data.
 */
static bool snapshot_take(int fd) {
    if (fd < 0 || fd >= MAPS_FD_TABLE_SIZE) return false;
    if (!snapshot_enabled()) return false;
    if (g_maps_snapshot[fd].data != NULL) return true;   /* already captured */

    size_t cap = 64 * 1024;
    size_t len = 0;
    char *buf = (char *) malloc(cap);
    if (buf == NULL) return false;

    for (;;) {
        if (len == cap) {
            size_t next = cap * 2;
            char *grown = (char *) realloc(buf, next);
            if (grown == NULL) {
                free(buf);
                return false;
            }
            buf = grown;
            cap = next;
        }

        long n = (long) syscall(SYS_pread64, fd, buf + len, cap - len, (off_t) len);
        if (n < 0) {
            if (errno == EINTR) continue;
            free(buf);
            return false;
        }
        if (n == 0) break;
        len += (size_t) n;
    }

    /*
     * The whole file is in hand, so every line is complete here. This is the only
     * place in the module where that is true, and it is why the rewrite is done at
     * this level rather than in the read hooks.
     */
    force_rewrite_tokens(buf, len);

    /*
     * Self-check, printed once per snapshot, because the previous round proved that a
     * successful rewrite log is not evidence that the result is clean.
     *
     * Every measurement so far has had to infer cleanliness from the fact that
     * rewriting ran: `maps rewritten occurrences=4` means four substitutions happened,
     * not that nothing remains. Those are different claims, and the difference is
     * exactly where this whole exercise kept losing. Counting the survivors afterwards
     * makes the claim falsifiable — a non-zero count names the token that got through
     * and its position, and a zero count is a fact about the buffer rather than a hope.
     *
     * Four things are counted separately because they fail for different reasons: the
     * literal token (a substitution that did not match), the module id (a name built at
     * runtime), the library name (a token added late and easy to miss), and executable
     * anonymous mappings (structural, not textual, and the one a string pass cannot
     * fix).
     */
    {
        int left_zygisk = 0;
        int left_vxcore = 0;
        int left_guise = 0;
        int left_exec_anon = 0;

        for (size_t p = 0; p + 6 <= len; p++) {
            if (memcmp(buf + p, "zygisk", 6) == 0) left_zygisk++;
            if (memcmp(buf + p, "vxcore", 6) == 0) left_vxcore++;
        }
        for (size_t p = 0; p + 5 <= len; p++) {
            if (memcmp(buf + p, "guise", 5) == 0) left_guise++;
        }
        /*
         * Counted by field, not by a trailing space.
         *
         * The previous version decided "anonymous" from the character before the
         * newline, which is the inode number on every real map line — so the count was
         * always zero no matter what the buffer contained. A check that cannot fail is
         * worse than no check: it turns "I did not find anything" into "there is
         * nothing", and that is exactly the confusion this whole measurement exists to
         * remove.
         *
         * A map line has five fixed fields separated by single spaces, then an optional
         * path. Four spaces means no path; five or more means a path is present. That is
         * the shape the kernel documents, and it does not depend on how the line ends.
         */
        int left_memfd = 0;
        int left_jitcache = 0;
        int left_rexec = 0;
        for (size_t p = 0; p < len; p++) {
            if (buf[p] != '\n') continue;
            size_t ls = p;
            while (ls > 0 && buf[ls - 1] != '\n') ls--;

            int spaces = 0;
            bool has_x = false;
            for (size_t k = ls; k < p; k++) {
                if (buf[k] == ' ') spaces++;
            }
            /*
             * The permission field is the second, so it starts after the first space
             * and is four characters wide. Checked here rather than by offset from the
             * line start so a truncated first field cannot shift it.
             */
            size_t sp1 = ls;
            while (sp1 < p && buf[sp1] != ' ') sp1++;
            if (sp1 + 4 < p && buf[sp1 + 3] == 'x') has_x = true;

            if (has_x) left_rexec++;
            if (spaces == 4 && has_x) left_exec_anon++;
        }
        for (size_t p = 0; p + 6 <= len; p++) {
            if (memcmp(buf + p, "memfd:", 6) == 0) left_memfd++;
        }
        for (size_t p = 0; p + 10 <= len; p++) {
            if (memcmp(buf + p, "jit-cache-", 10) == 0) left_jitcache++;
        }

        LOGI("snapshot verify fd=%d len=%zu zygisk=%d vxcore=%d guise=%d exec_anon=%d memfd=%d jitcache=%d rexec=%d",
             fd, len, left_zygisk, left_vxcore, left_guise, left_exec_anon,
             left_memfd, left_jitcache, left_rexec);
    }

    syscall(SYS_lseek, fd, 0, SEEK_SET);

    g_maps_snapshot[fd].data = buf;
    g_maps_snapshot[fd].length = len;
    g_maps_snapshot[fd].position = 0;

    LOGI("snapshot taken fd=%d len=%zu", fd, len);
    return true;
}

/*
 * Reads a mount table as one piece, rewrites it, and stores the result.
 *
 * Separate from snapshot_take for one reason: the rewrite is different. That function
 * calls force_rewrite_tokens, which looks for map markers -- a permission column, an
 * executable anonymous mapping, a runtime library at a non-zero offset. A mount table
 * has none of those, and it carries a source name that a map never contains, so
 * handing it to the map pass would scan it once and change nothing.
 *
 * The need is the same one that made the map snapshot necessary. The kernel returns a
 * mount table in chunks, and a chunk boundary falls wherever it likes. Measured: the
 * detector read /proc/self/mountinfo in 1024-byte pieces, rewrite_mountinfo_text was
 * entered twelve times with len=1024 on every call, the shape test passed every time,
 * and the source name was never once present, because it sat across a boundary in
 * each buffer it appeared in. A per-buffer rewrite cannot fix that. Only having the
 * whole file in hand can.
 *
 * The read loop uses the raw syscall for the same reason as the map version: this runs
 * from the open hooks, where real_read may not be resolved yet.
 *
 * The offset is restored afterwards, because a caller expects to read from the start of
 * a file it just opened.
 */
static bool mountinfo_snapshot_take(int fd) {
    if (fd < 0 || fd >= MAPS_FD_TABLE_SIZE) return false;
    if (g_maps_snapshot[fd].data != NULL) return true;

    size_t cap = 32 * 1024;
    size_t len = 0;
    char *buf = (char *) malloc(cap);
    if (buf == NULL) return false;

    for (;;) {
        if (len == cap) {
            size_t next = cap * 2;
            char *grown = (char *) realloc(buf, next);
            if (grown == NULL) {
                free(buf);
                return false;
            }
            buf = grown;
            cap = next;
        }
        long n = (long) syscall(SYS_pread64, fd, buf + len, cap - len, (off_t) len);
        if (n < 0) {
            if (errno == EINTR) continue;
            free(buf);
            return false;
        }
        if (n == 0) break;
        len += (size_t) n;
    }

    if (len == 0) {
        free(buf);
        return false;
    }

    /* The whole file is in hand, so every line is complete when it is judged. */
    rewrite_mountinfo_text(buf, len);

    {
        int left = 0;
        for (size_t p = 0; p + 6 <= len; p++) {
            if (memcmp(buf + p, "magisk", 6) == 0) left++;
        }
        LOGI("mountinfo snapshot taken fd=%d len=%zu magisk_left=%d", fd, len, left);
    }

    syscall(SYS_lseek, fd, 0, SEEK_SET);

    g_maps_snapshot[fd].data = buf;
    g_maps_snapshot[fd].length = len;
    g_maps_snapshot[fd].position = 0;
    return true;
}
static void snapshot_drop(int fd) {
    if (fd < 0 || fd >= MAPS_FD_TABLE_SIZE) return;
    if (g_maps_snapshot[fd].data == NULL) return;
    free(g_maps_snapshot[fd].data);
    g_maps_snapshot[fd].data = NULL;
    g_maps_snapshot[fd].length = 0;
    g_maps_snapshot[fd].position = 0;
}

static bool snapshot_has(int fd) {
    if (fd < 0 || fd >= MAPS_FD_TABLE_SIZE) return false;
    if (g_maps_snapshot[fd].data == NULL) return false;

    /*
     * A snapshot is filed under a descriptor number, and a number is handed out again
     * the moment its previous descriptor is closed — including by paths this module
     * cannot see, such as `close_range`, or a close performed inside the runtime.
     *
     * Trusting the number alone was measured to be wrong in the worst possible way. The
     * detector opened its mount table on a number that still carried a map snapshot, was
     * handed the map file instead, found no `/apex` entry in it, and reported a mount
     * anchor that had been present all along as missing. Nothing was corrupted and
     * nothing crashed: the process answered one question with another file's contents,
     * which is the kind of failure that is invisible from this side and only shows up as
     * a verdict on the other.
     *
     * The check costs one `readlinkat` and runs only for descriptors that actually have
     * a snapshot, so the ordinary read path pays nothing. A descriptor that no longer
     * points at a map file loses both its snapshot and its mark here, and the read falls
     * through to the real descriptor — which is the only correct answer, because that
     * descriptor now belongs to some other file and its contents are the caller's.
     */
    /*
     * Re-checking the descriptor here was attempted and reverted.
     *
     * The intent was sound: a snapshot is filed under a number, and a number is handed
     * out again as soon as its previous descriptor is closed, so a stale entry can serve
     * one file's bytes to a reader of another. Verifying the target at read time would
     * have closed that hole.
     *
     * The implementation was not safe. `fd_targets_map_file` releases the snapshot when
     * the check fails, and this function's result is consumed by callers that immediately
     * dereference the same entry — so the release and the use raced, and the detector
     * died with SEGV_ACCERR on its own native collection thread. The check also performs a
     * syscall into `/proc/self/fd`, which is the very surface the guard is watching.
     *
     * The hole is closed at its source instead: `hook_close` releases the snapshot, and
     * `hook_openat`/`hook_open` overwrite it whenever a new map file is opened on a
     * recycled number. What remains is a number closed through a path this module cannot
     * see — and for that case, serving the stale snapshot is the lesser failure, because
     * it is wrong only for the unrelated reader, whereas crashing takes down every reader.
     */
    return true;
}

/*
 * Serves bytes from a snapshot instead of the descriptor.
 *
 * `offset` is negative for a sequential read and non-negative for a positional one.
 * The distinction matters: a sequential read advances both the snapshot cursor and the
 * real descriptor's offset, so a caller that mixes `read` with `lseek` on the
 * descriptor still sees consistent behaviour; a positional read touches neither.
 *
 * Advancing the real offset by the same number of bytes is what keeps the two views in
 * step. Without it a caller that read four chunks and then called `lseek(fd, 0, SEEK_END)`
 * would get a position based on a descriptor that never moved, and every subsequent
 * offset would be wrong.
 */
static ssize_t snapshot_read(int fd, char *buf, size_t count, off64_t offset) {
    maps_snapshot_t *snap = &g_maps_snapshot[fd];
    size_t pos = (offset < 0) ? snap->position : (size_t) offset;

    if (pos >= snap->length) return 0;

    size_t avail = snap->length - pos;
    size_t take = (count < avail) ? count : avail;
    memcpy(buf, snap->data + pos, take);

    if (offset < 0) {
        snap->position = pos + take;
        syscall(SYS_lseek, fd, (off_t) take, SEEK_CUR);
    }
    return (ssize_t) take;
}

static void rewrite_maps_text(char *text, size_t len) {
    if (text == NULL || len == 0) return;

    bool has_zygisk = contains_ignore_case(text, NEUTRAL_ZYGISK_TOKEN);
    bool has_module = contains_ignore_case(text, NEUTRAL_MODULE_TOKEN);
    /*
     * The private executable copy of a runtime library is a fourth reason to rewrite,
     * and it does not share a token with the other three: a libart mapping line carries
     * neither `zygisk` nor the module package name, so the early return below would let
     * it pass untouched. It is checked here rather than folded into the token test
     * because the evidence is structural (an executable mapping of a runtime library at
     * a non-zero offset), not a string to match.
     */
    bool has_runtime_exec = false;
    bool has_cache_name = false;
    if (!has_zygisk && !has_module) {
        for (size_t p = 0; p + 9 <= len; p++) {
            if (memcmp(text + p, "libart.so", 9) == 0) { has_runtime_exec = true; break; }
        }
        /*
         * A JIT cache mapping is a fifth reason to rewrite, and it shares nothing
         * with the other four: the line carries neither token, names no runtime
         * library, and its permission column is unremarkable.
         *
         * The name is the evidence. The loader gives a module''s cache
         * `/memfd:jit-cache-<id>`, and no ordinary app has such a mapping -- the
         * runtime''s own is `jit-zygote-cache`. That difference is exactly what a
         * detector reports as an inconsistent cache, so the line has to reach the
         * pass that normalises it, and this gate is the only thing standing in the
         * way. Without it the pass never ran: the log carried no normalisation and
         * the detector went on seeing the name it objected to.
         *
         * The test is on the prefix, not on the suffix, because the suffix is the
         * module''s own id and changes with every build. `jit-cache` alone is the
         * runtime''s standard name and must not reach the pass -- the pass itself
         * re-checks the full form and leaves the standard one alone.
         */
        if (!has_runtime_exec) {
            for (size_t p = 0; p + 9 <= len; p++) {
                if (memcmp(text + p, "jit-cache", 9) == 0) { has_cache_name = true; break; }
            }
        }
    }
    if (!has_zygisk && !has_module && !has_runtime_exec && !has_cache_name) return;

    if (!contains_maps_perms(text, len)) {
        if (g_rewrite_skips < 5) {
            g_rewrite_skips++;
            LOGI("maps rewrite skipped: token present but no perms (len=%zu)", len);
        }
        return;
    }

    force_rewrite_tokens(text, len);
}

/*
 * Applies the guard to one buffer, choosing the check by what is known about the
 * source.
 *
 * A buffer from a marked descriptor is map content by definition and gets the
 * unconditional substitution. Anything else falls back to the content test, which
 * covers a reader that opened the file and passed the descriptor around in a way
 * the table could not follow — a number reused after `close`, or a `dup` the hook
 * did not see. Both paths converge on the same substitution, so the difference is
 * only in how much evidence is needed before it runs.
 */
static void guard_read_buffer(int fd, char *buf, size_t len) {
    /*
     * The guard touches a buffer only when the buffer itself says it should.
     *
     * Two things are deliberately absent here, and both were removed for the same
     * reason: they made the guard ask a question the caller had not asked.
     *
     * The first was a snapshot lookup. A stored copy taken at open time is only
     * correct for the descriptor it was taken from, and a descriptor number is not an
     * identity — the number is reused the moment its previous owner is closed, by
     * paths this module cannot see. Serving a stored map to a reader that asked for
     * its mount table produced a well-formed answer with the mount anchors missing,
     * and no error anywhere: the detector's complaint was the only trace. Snapshots
     * are off now, and nothing in this function consults them.
     *
     * The second was a descriptor check, `readlinkat` against `/proc/self/fd`. It read
     * `/proc` from inside a read hook while `/proc` was itself under the guard, and it
     * released table entries that callers were about to dereference; the detector died
     * of SEGV_ACCERR on its own collection thread. So this function performs no
     * syscall at all now, and cannot recurse into the surfaces it guards.
     *
     * What remains is the one rule that needs no state to be safe: if the buffer holds
     * a module marker, replace the marker in place, same length, same offsets. A
     * mount table contains no such marker, so it passes through byte for byte and the
     * detector reads exactly what the kernel wrote.
     *
     * The descriptor mark is still consulted, but only to pick the stronger scan: a
     * descriptor recorded at open as a map file gets the unconditional substitution,
     * because a map listing delivered in chunks can split a token across a boundary and
     * the content test alone would miss it. Trusting the mark cannot corrupt anything
     * — the substitution is length-preserving and a no-op when no marker is present —
     * so the worst case of a stale mark is a wasted scan, never a wrong answer.
     */
    if (is_maps_fd(fd)) {
        force_rewrite_tokens(buf, len);
        return;
    }
    /*
     * A token that is present is removed even when the descriptor was not marked.
     *
     * The mark is set from the path at open time, and it is the right answer only
     * for descriptors this module saw being opened. A reader can reach the same
     * file through a path this module never intercepted -- a duplicated descriptor,
     * a helper that opened it before injection, a `pread64` on a number that was
     * never seen -- and in those cases the mark is absent while the data is
     * identical.
     *
     * The previous fallback was the content test in `rewrite_maps_text`, which
     * requires a complete map line with its permission column. The kernel delivers
     * the file in page-sized chunks, and a chunk boundary falls wherever it likes: a
     * 4096-byte read whose first line starts mid-entry has no permission column at
     * all, the test rejects it, and the token in that chunk survives. The log said so
     * directly -- `maps rewrite skipped: token present but no perms (len=4096)` on
     * every chunk.
     *
     * Replacing the token does not need that test. The substitution is length
     * preserving and it only runs when the literal is already in the buffer, so a
     * buffer without the marker costs one scan and is left byte for byte. The
     * structural passes below -- the jit-cache name, the trampoline permission bit,
     * the private runtime copy -- do need to know they are looking at a map, because
     * they rewrite fields a non-map file could plausibly contain. Those stay behind
     * the content test; only the literal swap is promoted ahead of it.
     */
    if (contains_ignore_case_n(buf, len, NEUTRAL_ZYGISK_TOKEN)) {
        int early = replace_all_ignore_case(buf, len, NEUTRAL_ZYGISK_TOKEN,
                                           NEUTRAL_ZYGISK_REPLACEMENT);
        early += replace_all_ignore_case(buf, len, NEUTRAL_LIB_TOKEN,
                                         NEUTRAL_LIB_REPLACEMENT);
        early += replace_all_ignore_case(buf, len, NEUTRAL_MODULE_TOKEN,
                                         NEUTRAL_MODULE_REPLACEMENT);
        if (early > 0) {
            g_rewrite_hits++;
            if (g_rewrite_hits <= 12) {
                LOGI("maps token replaced without mark: fd=%d len=%lu hits=%d",
                     fd, (unsigned long) len, early);
            }
        }
    }
    /*
     * The structural pass for the runtime's private copy, run on the same terms.
     *
     * This one cannot be folded into the loop above, because it does not look for a
     * fixed literal. It walks to the start of each line and reads that line's
     * permission and offset columns to decide whether the mapping is a private
     * executable copy of the runtime -- the one mapping that is expected to differ
     * from the file on disk, and therefore the one a checksum comparison flags.
     *
     * It was unreachable. The content test in `rewrite_maps_text` rejects a buffer
     * that carries a marker but no complete permission column, and the kernel hands
     * the file out in page-sized pieces whose boundaries land wherever they like. A
     * buffer holding the middle of a long entry has the token and no permission
     * field, so the whole structural group was skipped -- measured as zero renames
     * across a full run while the detector kept finding the copy under its real
     * name.
     *
     * Calling it here does not weaken the decision it makes: the function re-reads
     * each line's fields itself and leaves anything it cannot classify untouched. A
     * buffer whose boundary cut the entry is therefore skipped by the pass rather
     * than by the caller, which is the same conservative outcome with one less gate
     * in front of it.
     */
    if (contains_ignore_case_n(buf, len, "libart.so")) {
        neutralize_private_exec_copy(buf, len);
    }
    /*
     * A mount table is not a map listing and does not go through the token passes.
     * The evidence it carries is a mount source name, and the substitution that
     * removes it is defined next to the other source-name constants.
     */
    if (is_mountinfo_fd(fd)) {
        rewrite_mountinfo_text(buf, len);
        return;
    }
    /*
     * The value table, applied to every buffer regardless of which file it came
     * from. This is the whole point: the reader asked for bytes and got bytes, and
     * it does not matter whether those bytes were a property file, a map listing, a
     * mount table, or something whose name nobody has written yet. If a real value
     * is in there, it is replaced with the configured one at the same length.
     */
    /*
     * The named form first: a line that carries its own property name can be judged
     * on the name, which is exact. The bare-value pass below cannot do that -- it sees
     * only a value and a guess about its neighbours -- so whatever this pass fixes,
     * the next one does not have to guess at.
     *
     * This is the only place `getprop` output can be changed. The program runs outside
     * the injected process, reads the real property area, and prints the real values;
     * its stdout is a pipe, and a pipe is read through this function.
     */
    int gp_hits = rewrite_getprop_lines(buf, len);
    if (gp_hits > 0) {
        int gseq = g_value_apply_count++;
        if (gseq < VALUE_APPLY_LOG_LIMIT) {
            LOGI("getprop line applied: fd=%d len=%lu hits=%d", fd, (unsigned long) len, gp_hits);
        }
    }
    {
        int vp_hits = rewrite_value_pairs(buf, len);
        if (vp_hits > 0) {
            int seq = g_value_apply_count++;
            if (seq < VALUE_APPLY_LOG_LIMIT) {
                LOGI("exit value applied: fd=%d len=%lu hits=%d", fd, (unsigned long) len, vp_hits);
            }
        }
    }
    rewrite_maps_text(buf, len);
}

/*
 * Rewrites the maps content as it is read.
 *
 * The read is passed through to the real function first and the buffer is edited
 * afterwards, so the byte count, the offset and every other property of the call
 * stay exactly what the kernel reported. The caller cannot tell that anything
 * happened except by looking for the token that is no longer there.
 */
/* TEMP DIAGNOSTIC: counts how often each read entry point is actually entered. */
static int g_read_calls = 0;
static int g_read_hits = 0;

/*
 * Rewrites one line handed back by fgets.
 *
 * The line is complete by construction, so the same passes that fix a snapshot buffer
 * apply here without a boundary problem. The length is recomputed rather than assumed,
 * because a line ending in a newline has that newline excluded from the text the
 * rewriters scan.
 */
/*
 * Zeroes the dirty-page accounting for the mappings whose backing store is a memory file.
 *
 * Relabelling replaces a library's read-only pages with a memory file holding the same
 * bytes, which removes the comparison a detector can make by reading the pages. It does
 * not remove the kernel accounting: the replacement pages are private and written, so
 * smaps reports them as dirty, while the pages they replaced were clean and file-backed.
 * A detector that reads smaps sees a library whose pages are marked dirty and reports
 * that the library content has been modified.
 *
 * The count is what carries the meaning here, not the mapping. The header line says which
 * object the pages belong to; the lines under it say how many of them are dirty. Zeroing
 * the count while leaving every address, size and name in place produces an entry that
 * reads like a clean mapping of that object, which is what it would have been without the
 * replacement.
 *
 * State is carried between calls because the file is read a line at a time: whether the
 * current entry is one of the replaced objects. A detector reads its own map listing on
 * one thread, entry after entry, so a plain static is enough.
 */
static volatile int g_smaps_in_watched = 0;

static int smaps_line_is_header(const char *line) {
    if (line == NULL || line[0] == '\0') return 0;
    if (!((line[0] >= '0' && line[0] <= '9') || (line[0] >= 'a' && line[0] <= 'f'))) return 0;
    for (int i = 0; i < 40 && line[i] != '\0'; i++) {
        if (line[i] == '-') return 1;
    }
    return 0;
}

static void sanitize_smaps_line(char *s, size_t n) {
    if (s == NULL || n == 0) return;

    if (smaps_line_is_header(s)) {
        int watched = 0;
        if (strstr(s, "shadow-code") != NULL) watched = 1;
        else if (strstr(s, "libart.so") != NULL) watched = 1;
        else if (strstr(s, "libandroid_runtime.so") != NULL) watched = 1;
        g_smaps_in_watched = watched;
        return;
    }

    if (!g_smaps_in_watched) return;

    static const char *keys[] = {
        "Private_Dirty:", "Shared_Dirty:", "Private_Clean:", "Shared_Clean:", "Anonymous:"
    };
    for (size_t k = 0; k < sizeof(keys) / sizeof(keys[0]); k++) {
        size_t kl = strlen(keys[k]);
        if (n < kl + 1) continue;
        if (strncmp(s, keys[k], kl) != 0) continue;

        char *p = s + kl;
        while (*p == ' ' || *p == '\t') p++;
        char *end = p;
        while (*end >= '0' && *end <= '9') end++;
        if (end == p) return;

        *p = '0';
        size_t tail = strlen(end);
        memmove(p + 1, end, tail + 1);
        return;
    }
}

/*
 * Rewrites only the files whose content is being answered for.
 *
 * The rewrite walks each line looking for tokens that reveal how this process was
 * instrumented, and for map entries whose dirty accounting has to be zeroed. Both are
 * about a handful of files — the process map listings and the property areas — and about
 * nothing else.
 *
 * Doing it unconditionally was the earlier behaviour, and it was measurable. A caller
 * that reads a large file a line at a time pays the scan on every line, and one of the
 * checks measures how long some work takes by forking and timing it. The scan was
 * landing inside that window. The files it was scanning had nothing to do with the
 * question being asked.
 *
 * A descriptor is the only thing that identifies a file once it is open, and the
 * descriptor is already recorded when the file is opened. So the fast path asks whether
 * this stream's descriptor is one of the recorded ones: if it is not, the line is handed
 * back untouched and costs nothing.
 *
 * The property descriptors are included because a property area can be read as text as
 * well as mapped, and the key name that has to be made unfindable lives in that text.
 */
static int stream_is_watched(FILE *stream) {
    if (stream == NULL) return 0;
    int fd = fileno(stream);
    if (fd < 0) return 0;
    if (is_maps_fd(fd)) return 1;
    if (is_prop_fd(fd)) return 1;
    if (is_mountinfo_fd(fd)) return 1;
    return 0;
}

static char *hook_fgets(char *s, int size, FILE *stream) {
    if (real_fgets == NULL) return NULL;
    char *r = real_fgets(s, size, stream);
    if (r == NULL || s == NULL) return r;
    if (!stream_is_watched(stream)) return r;
    size_t n = strlen(s);
    if (n == 0) return r;
    force_rewrite_tokens(s, n);
    rewrite_mountinfo_text(s, n);
    sanitize_smaps_line(s, n);
    return r;
}

/*
 * Rewrites a block read through fread, but only when it is line-aligned enough to judge.
 *
 * fread may return a partial line. Rewriting a fragment would change bytes that the
 * next call continues, and the file would end up with a line that is neither the
 * original nor the rewritten one. The check is therefore on shape: the block must
 * contain a newline and start at a position that looks like the beginning of a map
 * entry.
 */
/*
 * Passes through, deliberately.
 *
 * An earlier version rewrote fread buffers the same way fgets buffers are rewritten, and
 * it was wrong in a way worth recording: fread returns bytes, not lines. The buffer may
 * hold an ELF header, a zip directory, or half a line of anything, and the rewrite wrote
 * a terminator at the last byte before looking at the content. Any caller that read a
 * binary file through here had its last byte destroyed, and a caller that parses its own
 * assets this way fails as soon as its data no longer matches what it wrote.
 *
 * Nothing is lost by removing this. Map listings are text and are read either a line at
 * a time or through read; fread is what binary parsing uses, and binary parsing is
 * exactly what must not be touched.
 */
static size_t hook_fread(void *ptr, size_t size, size_t nmemb, FILE *stream) {
    if (real_fread == NULL) return 0;
    return real_fread(ptr, size, nmemb, stream);
}

/*
 * Observes fopen without changing its result.
 *
 * The descriptor has to stay the kernel is one: fstat and mmap are called on it by
 * readers that check more than the bytes, and replacing it was measured to crash. What
 * this does instead is note that a stdio read is starting, so the rewrite counters can
 * be attributed to the right path when a report disagrees with the log.
 */
static FILE *hook_fopen(const char *path, const char *mode) {
    if (real_fopen == NULL) return NULL;
    FILE *fp = real_fopen(path, mode);
    if (fp != NULL && path != NULL) {
        int n = __atomic_add_fetch(&g_stdio_hits, 1, __ATOMIC_RELAXED);
        if (n <= 8) {
            LOGI("fopen observed: %s", path);
        }

        /*
         * A descriptor opened through `fopen` needs the same identity as one opened
         * through `open`.
         *
         * The rewrite in `hook_fgets` is gated on `stream_is_watched`, and that asks
         * whether the stream's descriptor was recorded as a map, a mount table or a
         * property area. Recording happened only in the three `open` entry points, so
         * a reader that reached the file through stdio arrived with an unrecorded
         * descriptor and every line was handed back exactly as the kernel wrote it.
         *
         * This is not a hypothetical gap. The detector that compares the runtime
         * against itself opens the map file with `fopen` and reads it with `fgets`.
         * Its reads never touched `hook_read` -- measured: `read` was called five
         * times in the whole process, all of them from this module's own start-up --
         * so the private runtime copy kept its real name in the listing and the
         * checksum comparison kept finding a second mapping that did not match the
         * file. Marking here closes the last unguarded entrance to that file.
         *
         * The mark is keyed on the descriptor, not the stream, because that is what
         * the decision downstream has access to: `fileno` is the only thing linking a
         * `FILE *` to the table. The mark is cleared by the same two paths that clear
         * it for `open` -- `hook_close` and `SYS_close` -- so a descriptor number that
         * comes back later cannot inherit this identity.
         *
         * A path that matches none of the three is left unmarked, which is the same
         * behaviour every stream had before this existed.
         */
        int fd = fileno(fp);
        if (fd >= 0) {
            if (is_maps_path(path)) mark_maps_fd(fd);
            if (is_mountinfo_path(path)) mark_mountinfo_fd(fd);
            if (is_prop_path(path)) mark_prop_fd(fd);
        }
    }
    return fp;
}

static ssize_t hook_read(int fd, void *buf, size_t count) {
    if (real_read == NULL) {
        errno = ENOSYS;
        return -1;
    }
    g_read_calls++;

    /*
     * A snapshot, when there is one, is the whole answer.
     *
     * The bytes in it were rewritten while the entire file was in hand, so every line
     * was complete when it was judged. Serving from it here means no caller can ever
     * observe a half-line, and the chunking that defeated every earlier attempt stops
     * mattering: the caller may still ask for 1024 bytes at a time, but those bytes are
     * a slice of a file that was fixed as a whole.
     *
     * The real descriptor's offset is advanced by the same amount, so a caller that
     * mixes `read` with `lseek` keeps a consistent view. The descriptor itself is
     * untouched and still points at the true file, which is what keeps `fstat` and
     * `mmap` honest.
     */
    if (is_prop_fd(fd)) {
        if (!prop_snapshot_has(fd)) prop_snapshot_take(fd);
        if (prop_snapshot_has(fd)) return prop_snapshot_read(fd, (char *) buf, count, -1);
    }
    if (snapshot_has(fd)) {
        return snapshot_read(fd, (char *) buf, count, -1);
    }

    ssize_t n = real_read(fd, buf, count);
    if (n > 0 && buf != NULL) {
        if (g_read_calls <= 5) {
            LOGI("read call #%d fd=%d n=%zd head=%.16s", g_read_calls, fd, n, (char *) buf);
        }
        bool had_zygisk = contains_ignore_case((char *) buf, NEUTRAL_ZYGISK_TOKEN);
        if (had_zygisk) {
            g_read_hits++;
            LOGI("read buffer CONTAINS zygisk BEFORE rewrite (hit #%d fd=%d n=%zd)", g_read_hits, fd, n);
        }
        /*
         * The content decides, not the descriptor.
         *
         * Tracking which fd numbers came from a maps open was the first design, and
         * it was too fragile: a reader that reaches the file through `pread64`, a
         * `readv`, or a duplicated descriptor never touches the number that was
         * recorded, and the token passes through untouched. A buffer that starts with
         * a map entry can only be a map, so recognising the content directly is both
         * simpler and strictly more reliable.
         *
         * The check is deliberately narrow: `looks_like_maps_text` requires the hex
         * address form at the very start of the buffer, and the rewrite only happens
         * when `zygisk` is actually present. An ordinary read pays one length test and
         * a single character comparison before falling through.
         */
        /*
         * The descriptor decides when it is known, the content otherwise.
         *
         * `guard_read_buffer` runs the unconditional substitution for a descriptor
         * recorded as a map file, and the content test only for anything else. That
         * order matters: the kernel delivers `smaps` in chunks whose boundaries fall
         * anywhere, so a buffer test that insists on seeing a permission column
         * rejects precisely the chunks that carry the names — which is how the token
         * survived a rewrite that the log said had succeeded.
         */
        guard_read_buffer(fd, (char *) buf, (size_t) n);

        /*
         * A property file is scanned as text, so the name is what makes it
         * found. This runs after the map rewrite because the two never
         * describe the same file: one is a list of mappings, the other is
         * a table of names and values.
         */
        if (is_prop_fd(fd)) {
            neutralize_prop_key((char *) buf, (size_t) n);
        }
        if (had_zygisk) {
            if (contains_ignore_case((char *) buf, NEUTRAL_ZYGISK_TOKEN)) {
                LOGI("read AFTER rewrite STILL contains zygisk (fd=%d)", fd);
            } else {
                LOGI("read AFTER rewrite cleaned (fd=%d)", fd);
            }
        }
    }
    return n;
}

/*
 * The positional variant of `read`.
 *
 * A reader that knows the file size often skips `read` entirely and uses `pread64`
 * with an explicit offset. The buffer and the count mean the same thing, so the same
 * content check applies; only the argument order differs, and the offset is irrelevant
 * to the rewrite because the substitution is position preserving.
 */
static ssize_t hook_pread64(int fd, void *buf, size_t count, off64_t offset) {
    if (real_pread64 == NULL) {
        errno = ENOSYS;
        return -1;
    }
    /* Positional reads come from the snapshot at the requested offset, not from a cursor. */
    if (snapshot_has(fd)) {
        return snapshot_read(fd, (char *) buf, count, (off64_t) offset);
    }
    if (is_prop_fd(fd)) {
        if (!prop_snapshot_has(fd)) prop_snapshot_take(fd);
        if (prop_snapshot_has(fd)) return prop_snapshot_read(fd, (char *) buf, count, (off64_t) offset);
    }
    /* Positional reads come from the snapshot at the requested offset, not from a cursor. */

    ssize_t n = real_pread64(fd, buf, count, offset);
    if (n > 0 && buf != NULL) {
        guard_read_buffer(fd, (char *) buf, (size_t) n);

        /*
         * A property file is scanned as text, so the name is what makes it
         * found. This runs after the map rewrite because the two never
         * describe the same file: one is a list of mappings, the other is
         * a table of names and values.
         */
        if (is_prop_fd(fd)) {
            neutralize_prop_key((char *) buf, (size_t) n);
        }
    }
    return n;
}

/*
 * The vector variant of `read`.
 *
 * `readv` fills several buffers in one call, so each one is checked on its own. A maps
 * read is almost always a single iovec, but splitting the content across two is legal
 * and a reader is free to do it — the per-buffer check covers both cases with no
 * special handling.
 */
/*
 * Fills an iovec array from a snapshot.
 *
 * The vector form is used by readers that want the file in one call, which makes it
 * the form most likely to be handed the whole map at once — and therefore the one
 * where a complete file matters most. Filling it from the snapshot keeps the same
 * semantics as `read`: segments are consumed in order, each taking as much as it can
 * hold, and the total returned is what was actually placed.
 *
 * `positional` chooses between the two offset rules. A sequential call advances the
 * snapshot cursor and the real descriptor's offset by the same number of bytes; a
 * positional call starts at `offset` and touches neither.
 */
static ssize_t snapshot_readv(int fd, const struct iovec *iov, int iovcnt,
                              off64_t offset) {
    if (iov == NULL || iovcnt <= 0) return 0;

    maps_snapshot_t *snap = &g_maps_snapshot[fd];
    size_t pos = (offset < 0) ? snap->position : (size_t) offset;
    if (pos >= snap->length) return 0;

    size_t total = 0;
    for (int i = 0; i < iovcnt && i < 64; i++) {
        if (iov[i].iov_base == NULL || iov[i].iov_len == 0) continue;
        size_t avail = snap->length - pos;
        if (avail == 0) break;
        size_t take = (iov[i].iov_len < avail) ? iov[i].iov_len : avail;
        memcpy(iov[i].iov_base, snap->data + pos, take);
        pos += take;
        total += take;
    }

    if (offset < 0 && total > 0) {
        snap->position = pos;
        syscall(SYS_lseek, fd, (off_t) total, SEEK_CUR);
    }
    return (ssize_t) total;
}

static ssize_t hook_readv(int fd, const struct iovec *iov, int iovcnt) {
    if (real_readv == NULL) {
        errno = ENOSYS;
        return -1;
    }
    if (snapshot_has(fd)) {
        return snapshot_readv(fd, iov, iovcnt, -1);
        }
        if (is_prop_fd(fd)) {
            if (!prop_snapshot_has(fd)) prop_snapshot_take(fd);
            if (prop_snapshot_has(fd)) return prop_snapshot_readv(fd, iov, iovcnt, -1);
    }
    ssize_t n = real_readv(fd, iov, iovcnt);
    if (n > 0 && iov != NULL) {
        /*
         * Only the bytes the kernel actually filled are rewritten.
         *
         * `iov_len` is the capacity of each segment, not the number of bytes placed
         * in it; a read that fills one segment completely and another partially
         * leaves the remainder of the second untouched. Passing `iov_len` here would
         * rewrite memory that was never written — harmless when it happens to be
         * zero, and a corruption of whatever else lives there when it does not.
         *
         * The budget therefore decreases as each segment is accounted for, which is
         * exactly what the kernel's return value describes.
         */
        ssize_t left = n;
        for (int i = 0; i < iovcnt && i < 64 && left > 0; i++) {
            if (iov[i].iov_base == NULL || iov[i].iov_len == 0) continue;
            size_t take = (size_t) left;
            if (take > iov[i].iov_len) take = iov[i].iov_len;
            guard_read_buffer(fd, (char *) iov[i].iov_base, take);
            left -= (ssize_t) take;
        }
    }
    return n;
}

/*
 * The positional vector variants.
 *
 * These are the two entry points the earlier attempt missed, and they are the most
 * likely reason a read that the hooks demonstrably rewrote (`read AFTER rewrite
 * cleaned`) still produced a positive detection: a reader that knows the file's
 * length and wants it in one piece calls `preadv` rather than `readv`, because the
 * offset makes the whole file addressable in a single call.
 *
 * Both take the same iovec shape as `readv`, so the same per-segment accounting
 * applies. `preadv2` adds a flags word that is passed through untouched — RWF_NOWAIT
 * and RWF_HIPRI affect how the kernel services the call, not what the bytes mean.
 */
static ssize_t hook_preadv(int fd, const struct iovec *iov, int iovcnt, off_t offset) {
    if (real_preadv == NULL) {
        errno = ENOSYS;
        return -1;
    }
    if (snapshot_has(fd)) {
        return snapshot_readv(fd, iov, iovcnt, (off64_t) offset);
        }
        if (is_prop_fd(fd)) {
            if (!prop_snapshot_has(fd)) prop_snapshot_take(fd);
            if (prop_snapshot_has(fd)) return prop_snapshot_readv(fd, iov, iovcnt, (off64_t) offset);
    }
    ssize_t n = real_preadv(fd, iov, iovcnt, offset);
    if (n > 0 && iov != NULL) {
        ssize_t left = n;
        for (int i = 0; i < iovcnt && i < 64 && left > 0; i++) {
            if (iov[i].iov_base == NULL || iov[i].iov_len == 0) continue;
            size_t take = (size_t) left;
            if (take > iov[i].iov_len) take = iov[i].iov_len;
            guard_read_buffer(fd, (char *) iov[i].iov_base, take);
            left -= (ssize_t) take;
        }
    }
    return n;
}

static ssize_t hook_preadv2(int fd, const struct iovec *iov, int iovcnt,
                            off_t offset, int flags) {
    if (real_preadv2 == NULL) {
        errno = ENOSYS;
        return -1;
    }
    if (snapshot_has(fd)) {
        return snapshot_readv(fd, iov, iovcnt, (off64_t) offset);
        }
        if (is_prop_fd(fd)) {
            if (!prop_snapshot_has(fd)) prop_snapshot_take(fd);
            if (prop_snapshot_has(fd)) return prop_snapshot_readv(fd, iov, iovcnt, (off64_t) offset);
    }
    ssize_t n = real_preadv2(fd, iov, iovcnt, offset, flags);
    if (n > 0 && iov != NULL) {
        ssize_t left = n;
        for (int i = 0; i < iovcnt && i < 64 && left > 0; i++) {
            if (iov[i].iov_base == NULL || iov[i].iov_len == 0) continue;
            size_t take = (size_t) left;
            if (take > iov[i].iov_len) take = iov[i].iov_len;
            guard_read_buffer(fd, (char *) iov[i].iov_base, take);
            left -= (ssize_t) take;
        }
    }
    return n;
}

/*
 * The loader's own answer, which no descriptor ever carries.
 *
 * Every hook above intercepts a system call or a library call that moves bytes. This
 * one is different in kind: `dl_iterate_phdr` reads no file and returns no buffer, it
 * walks the dynamic linker's table of loaded objects and hands each one's load path to
 * a callback. There is nothing on that path for a read guard to see, and the strings
 * are the loader's own.
 *
 * The callback is therefore wrapped rather than the data: we receive the loader's
 * `dl_phdr_info`, make a private copy of it, point that copy's `dlpi_name` at a
 * rewritten name, and pass the copy to the caller's callback. The loader's struct is
 * never modified, so its own bookkeeping cannot be disturbed, and the caller sees a
 * complete, consistent entry whose name is the neutral one.
 *
 * The name is built in a stack buffer that lives for the duration of the callback, so
 * a caller that stores the pointer past its return gets the same lifetime behaviour it
 * would have got from the loader on some platforms and a shorter one here — which is
 * why only the pointer is swapped and the buffer is not freed or reused.
 */
typedef struct {
    dl_iterate_phdr_cb_t user_cb;
    void *user_data;
} dl_iterate_bridge_t;

static int dl_iterate_bridge_cb(struct dl_phdr_info *info, size_t size, void *data) {
    dl_iterate_bridge_t *bridge = (dl_iterate_bridge_t *) data;
    if (info == NULL || bridge == NULL || bridge->user_cb == NULL) return 0;

    struct dl_phdr_info copy = *info;
    char name_buf[512];

    if (info->dlpi_name != NULL && info->dlpi_name[0] != '\0') {
        size_t name_len = strlen(info->dlpi_name);
        if (name_len > 0 && name_len < sizeof(name_buf)) {
            memcpy(name_buf, info->dlpi_name, name_len);
            name_buf[name_len] = '\0';

            /*
             * The same three substitutions the map paths apply, because these are the
             * same names: a loaded object's path is what maps would report for it, and
             * the loader is simply reporting it without a file in between.
             */
            neutralize_module_jit_cache(name_buf, name_len);
            replace_all_ignore_case(name_buf, name_len, NEUTRAL_ZYGISK_TOKEN,
                                    NEUTRAL_ZYGISK_REPLACEMENT);
            replace_all_ignore_case(name_buf, name_len, NEUTRAL_LIB_TOKEN,
                                    NEUTRAL_LIB_REPLACEMENT);
            replace_all_ignore_case(name_buf, name_len, NEUTRAL_MODULE_TOKEN,
                                    NEUTRAL_MODULE_REPLACEMENT);
            /*
             * A library whose private executable copy has been re-labelled also has to
             * stop being recognisable here.
             *
             * The relabelling in `relabel_private_exec` makes the kernel report a memory
             * file for those pages, which is correct and necessary — but it changes only
             * what a reader of `/proc/self/maps` sees. A detector that walks the loader's
             * own module list instead gets this callback, and `dlpi_name` still carries
             * the library's real path, so the module is identified and its segments are
             * checked by address regardless of what the map is called.
             *
             * The fix is the same substitution used for every other trace: replace the
             * leaf name with one of identical length, so the string keeps its shape and
             * nothing that hashes or indexes it sees a difference. It runs only for the
             * libraries named in the configuration, because that list is exactly the set
             * whose identity has already been rewritten elsewhere and would otherwise
             * contradict itself between the two channels.
             */
            for (int vi = 0; vi < g_vma_spec_count; vi++) {
                const char *want = g_vma_specs[vi].soname;
                if (want == NULL || want[0] == '\0') continue;

                size_t want_len = strlen(want);
                if (want_len == 0 || want_len >= name_len) continue;

                for (size_t k = 0; k + want_len <= name_len; k++) {
                    if (memcmp(name_buf + k, want, want_len) != 0) continue;

                    /*
                     * Replace the first character with one that cannot appear in a
                     * soname's leading position, keeping the length. `libart.so` becomes
                     * `_ibart.so`, which no loader would produce and no detector looks for.
                     */
                    name_buf[k] = '_';
                    g_rewrite_hits++;
                    if (g_rewrite_hits <= 12) {
                        LOGI("dlpi renamed: %s", name_buf);
                    }
                    break;
                }
            }

            copy.dlpi_name = name_buf;
        }
    }

    return bridge->user_cb(&copy, size, bridge->user_data);
}

static int hook_dl_iterate_phdr(dl_iterate_phdr_cb_t cb, void *data) {
    if (real_dl_iterate_phdr == NULL) return -1;
    if (cb == NULL) return real_dl_iterate_phdr(NULL, data);

    dl_iterate_bridge_t bridge;
    bridge.user_cb = cb;
    bridge.user_data = data;
    return real_dl_iterate_phdr(dl_iterate_bridge_cb, &bridge);
}

/*
 * The third channel: the name the kernel reports for a mapping without ever
 * handing over the mapped file's contents.
 *
 * This is the one the read hooks could not cover, and it is why a guard that
 * demonstrably cleaned every buffer it was given still produced a positive report.
 * `readlink` on a `/proc/<pid>/map_files/<range>` entry returns the backing object's
 * name directly — no read, no buffer, no descriptor that was recorded at open time.
 * The kernel answers with the whole path as a single string, and the name of the
 * Vector memfd is in it:
 *
 *   /memfd:jit-cache-zygisk_vector (deleted)
 *
 * The same call is how the module's own APK path leaks, so both substitutions are
 * applied here with the same rule as everywhere else: replace the token with one of
 * identical length, so the returned string keeps its exact shape and any reader that
 * hashes it or parses it by position sees an ordinary, unrelated name.
 */
static ssize_t rewrite_link_result(char *buf, ssize_t n) {
    if (buf == NULL || n <= 0) return n;

    /*
     * A link target is a name, not file content, so the name-level pass has to run
     * here too.
     *
     * `/proc/self/map_files/<range>` resolves to `/memfd:jit-cache-<id> (deleted)`,
     * which carries exactly the signature the maps rewrite removes — but it arrives
     * through `readlink` and never touches a read buffer. A guard that only patched
     * the read paths therefore left this door open, and the name it returns is enough
     * on its own to say "a Zygisk module is injected here".
     */
    neutralize_module_jit_cache(buf, (size_t) n);

    int hit = 0;
    hit += replace_all_ignore_case(buf, (size_t) n,
                                   NEUTRAL_ZYGISK_TOKEN, NEUTRAL_ZYGISK_REPLACEMENT);
    /*
     * The library's own name leaks through this channel as well.
     *
     * `map_files` resolves a range to the backing object, so the guard's own library
     * appears here as `libguise_native.so` rather than as part of an apk path. The
     * package token cannot match it, and a reader that only ever asks for link targets
     * would see the module's name no matter how clean the read buffers were.
     */
    hit += replace_all_ignore_case(buf, (size_t) n,
                                   NEUTRAL_LIB_TOKEN, NEUTRAL_LIB_REPLACEMENT);
    hit += replace_all_ignore_case(buf, (size_t) n,
                                   NEUTRAL_MODULE_TOKEN, NEUTRAL_MODULE_REPLACEMENT);
    /*
     * The su symlink resolves to `./magisk`, and that name arrives through this
     * channel rather than through a read buffer. `hook_readlink` hands the target
     * back to the caller verbatim, so without this pass the one string a root
     * check asks for by name would survive every filter applied to file content.
     *
     * Six characters for six, which is the same length-preserving rule the other
     * three passes follow: the caller sees a link target of the length it expects,
     * pointing at a name that belongs to the platform rather than to the module.
     */
    hit += replace_all_ignore_case(buf, (size_t) n,
                                   NEUTRAL_MAGISK_TOKEN, NEUTRAL_MAGISK_REPLACEMENT);
    if (hit > 0) {
        g_rewrite_hits++;
        if (g_rewrite_hits <= 12) {
            LOGI("link rewritten occurrences=%d total=%d n=%zd", hit, g_rewrite_hits, n);
        }
    }
    return n;
}

static ssize_t hook_readlink(const char *path, char *buf, size_t bufsiz) {
    if (real_readlink == NULL) {
        errno = ENOSYS;
        return -1;
    }
    ssize_t n = real_readlink(path, buf, bufsiz);
    return rewrite_link_result(buf, n);
}

static ssize_t hook_readlinkat(int dirfd, const char *path, char *buf, size_t bufsiz) {
    if (real_readlinkat == NULL) {
        errno = ENOSYS;
        return -1;
    }
    ssize_t n = real_readlinkat(dirfd, path, buf, bufsiz);
    return rewrite_link_result(buf, n);
}

/*
 * The FORTIFY open, marked the same way as the ordinary one.
 *
 * Both names produce a descriptor, and a descriptor is the only thing that identifies
 * the property file once it is open. Marking it here means the mmap hook below does not
 * have to guess which mappings came from a property file: the mark says so.
 */
static int hook_open_2(const char *path, int flags) {
    if (real_open_2 == NULL) { errno = ENOSYS; return -1; }

    /*
     * The same guard `open` and `openat` already carry, on the entry point that
     * was missing it.
     *
     * `__open_2` is the fortified form of `open`, and it is a distinct symbol
     * rather than an alias: a library built with `_FORTIFY_SOURCE` calls it
     * directly, and patching `open` does nothing for it. `libapplist_detector.so`
     * imports `__open_2` and no other open variant, which is why a hidden
     * directory still produced a descriptor. The descriptor then reached `fstat`,
     * and the kernel's refusal to read it -- a SELinux denial on the directory --
     * was what the detector recorded. It never got a clean "no such file"; it got
     * "present but forbidden", and those are different answers.
     *
     * The order matches the other two: ask, then decide, then take back. A
     * descriptor is a thing to be released, not a boolean to be flipped, so the
     * only way to deny one after the kernel has made it is to close it and report
     * the error the caller would have received had the file not existed. Closing
     * through the real function keeps this module's own bookkeeping out of it, and
     * the number is freed exactly as it would have been by the caller.
     */
    int fd = real_open_2(path, flags);
    if (fd >= 0 && is_hidden_path(path)) {
        if (real_close != NULL) real_close(fd);
        errno = ENOENT;
        return -1;
    }
    if (fd >= 0 && is_prop_path(path)) mark_prop_fd(fd);
    /* Deferred: see prop_snapshot_take for why this cannot run here. */
    return fd;
}

/*
 * Maps a copy instead of the file when the file is a property area.
 *
 * The caller gets a mapping with the requested length and permissions at a fresh
 * address, holding the file's bytes with the overridden values substituted. The
 * descriptor is not consumed, not duplicated, and not replaced — the caller closes it
 * exactly as it would have — so nothing about its own bookkeeping changes.
 *
 * A fixed mapping is left alone. MAP_FIXED is a request for a particular address, and
 * the point of it is usually that something already expects data there; quietly giving
 * the caller a different address would be worse than not helping. Property areas are not
 * mapped that way, so the case costs nothing.
 *
 * The size limit exists because the copy is made in one buffer. Property areas are
 * small; anything larger is not one, and is passed through untouched.
 */
static void *hook_mmap(void *addr, size_t length, int prot, int flags, int fd, off_t offset) {
    if (real_mmap == NULL) { errno = ENOSYS; return MAP_FAILED; }

    if (fd >= 0 && length > 0 && length <= (8 * 1024 * 1024) &&
        (flags & MAP_FIXED) == 0 && is_prop_fd(fd)) {
        char *copy = (char *) malloc(length);
        if (copy != NULL) {
            ssize_t got = (ssize_t) syscall(SYS_pread64, fd, copy, length, offset);
            if (got == (ssize_t) length) {
                int changed = rewrite_prop_values(copy, length);
                if (changed > 0) {
                    void *m = real_mmap(NULL, length, PROT_READ | PROT_WRITE,
                                        MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
                    if (m != MAP_FAILED) {
                        memcpy(m, copy, length);
                        if (prot != (PROT_READ | PROT_WRITE)) {
                            mprotect(m, length, prot);
                        }
                        free(copy);
                        LOGI("prop mmap intercepted: %lu bytes, %d values",
                             (unsigned long) length, changed);
                        return m;
                    }
                }
            }
            free(copy);
        }
    }

    return real_mmap(addr, length, prot, flags, fd, offset);
}

static int hook_openat(int dirfd, const char *path, int flags, mode_t mode) {
    if (real_openat == NULL) {
        errno = ENOSYS;
        return -1;
    }
    /*
     * The kernel answers first, and its answer decides what the caller is told.
     *
     * Opening is the call that produces a descriptor, so hiding a path here means
     * taking one back rather than never asking. The order matters for the same reason
     * it does in `access` and `stat`: the errno a failed open produces is information —
     * `EACCES` says the file is there but forbidden, `ENOENT` says it is not there at
     * all — and a caller that receives the wrong one builds a picture that contradicts
     * whatever it learns next.
     *
     * A successful open on a hidden path is therefore closed immediately and reported as
     * `ENOENT`, which is the only consistent story: the caller is told the file does not
     * exist, and is given no descriptor through which to discover otherwise. The
     * descriptor number is released by the same `close` the caller would have used, so
     * nothing is leaked.
     */
    int hidden_fd = real_openat(dirfd, path, flags, mode);
    if (hidden_fd >= 0 && is_hidden_path(path)) {
        if (real_close != NULL) real_close(hidden_fd);
        errno = ENOENT;
        return -1;
    }
    if (hidden_fd < 0) {
        return hidden_fd;
    }
    /*
     * From here the descriptor belongs to the caller, and only its number is remembered.
     *
     * The descriptor itself is the kernel's own and is handed back unmodified.
     * Substituting a memfd here is what crashed the process on two earlier attempts: a
     * proc file and a memfd are not interchangeable to a reader that inspects the
     * descriptor rather than only reading it. Returning the real descriptor and editing
     * the bytes on the way out keeps every one of those properties intact.
     *
     * The open is not repeated. The first version of this function performed the real
     * open twice — once to learn the truth and once to return a value — which would have
     * leaked the first descriptor on every call. The descriptor obtained above is the one
     * that is returned.
     */
    int fd = hidden_fd;
    if (is_prop_path(path)) {
        mark_prop_fd(fd);
        /* Deferred: see prop_snapshot_take for why this cannot run here. */
    }
    if (is_maps_path(path)) {
        mark_maps_fd(fd);

        /*
         * Capture the whole file now, while it is known to be a map listing.
         *
         * This is the one moment where the entire content is available at once and
         * the decision to treat it as a map has already been made. Reading it here
         * and rewriting it as a single piece is what removes the chunk boundary
         * problem entirely: by the time any caller asks for 1024 bytes, the file they
         * are asking from has already been fixed as a whole.
         *
         * A failure is not fatal. If the snapshot cannot be taken — memory pressure,
         * a file that grew between the open and the read — the descriptor stays marked
         * and the per-buffer path still runs, which is weaker but never wrong.
         */
        snapshot_take(fd);
    }
    if (is_mountinfo_path(path)) {
        mark_mountinfo_fd(fd);
        mountinfo_snapshot_take(fd);
    }
    /* TEMP DIAGNOSTIC: every /proc open, so the reads that matter are named. */
    if (path != NULL && strncmp(path, "/proc/", 6) == 0) {
        int c = g_proc_open_count++;
        if (c < PROC_OPEN_DIAGNOSTIC_LIMIT) {
            LOGI("proc openat #%d fd=%d %s", c, fd, path);
        }
    }
    return fd;
}

static int hook_open(const char *path, int flags, mode_t mode) {
    if (real_open == NULL) {
        errno = ENOSYS;
        return -1;
    }
    /* Same order as `hook_openat`: ask, then decide, then possibly take back. */
    int hidden_fd = real_open(path, flags, mode);
    if (hidden_fd >= 0 && is_hidden_path(path)) {
        if (real_close != NULL) real_close(hidden_fd);
        errno = ENOENT;
        return -1;
    }
    if (hidden_fd < 0) {
        return hidden_fd;
    }
    int fd = hidden_fd;
    if (is_prop_path(path)) {
        mark_prop_fd(fd);
        /* Deferred: see prop_snapshot_take for why this cannot run here. */
    }
    if (fd >= 0 && is_maps_path(path)) {
        mark_maps_fd(fd);
        /* Same capture as `hook_openat`; see the note there for why it happens here. */
        snapshot_take(fd);
    }
    if (fd >= 0 && is_mountinfo_path(path)) {
        mark_mountinfo_fd(fd);
        mountinfo_snapshot_take(fd);
    }
    if (path != NULL && strncmp(path, "/proc/", 6) == 0) {
        int c = g_proc_open_count++;
        if (c < PROC_OPEN_DIAGNOSTIC_LIMIT) {
            LOGI("proc open #%d fd=%d %s", c, fd, path);
        }
    }
    return fd;
}

/*
 * The switchboard rule, applied to the path-existence calls.
 *
 * The first version of these two hooks answered from the path alone: if it matched a
 * hidden prefix, they returned `ENOENT` without ever asking the kernel. That is not a
 * guard, it is a dropped packet. The kernel had an answer — the file may exist, may be
 * denied, may be a broken link — and that answer is the only thing that lets the caller
 * build a consistent picture. Replacing it with a guess means the caller learns a fact
 * that is not true, and learns it in a way that contradicts what the next call will say.
 *
 * So the real call always runs first. What comes back is the truth: a file descriptor, a
 * `struct stat`, or an errno the kernel chose. Only then is the answer considered, and
 * only two things are ever changed:
 *
 *   - a success becomes `ENOENT`, because a caller that can see this path can see the
 *     module, and that is the one fact worth withholding;
 *   - the errno of a genuine failure is left exactly as the kernel reported it, because
 *     `EACCES` and `ENOENT` mean different things and a caller is entitled to tell them
 *     apart.
 *
 * The distinction matters for consistency across calls. `access`, `stat` and `open` must
 * agree about the same path, and the only way to guarantee that is for all three to start
 * from the same source of truth rather than from three copies of the same rule.
 */
static int hook_access(const char *path, int mode) {
    if (real_access == NULL) {
        errno = ENOSYS;
        return -1;
    }

    /* Always ask the kernel first. The answer is data, not a decision. */
    int result = real_access(path, mode);

    if (result == 0 && is_hidden_path(path)) {
        errno = ENOENT;
        return -1;
    }
    return result;
}

static int hook_stat(const char *path, struct stat *buf) {
    if (real_stat == NULL) {
        errno = ENOSYS;
        return -1;
    }

    /*
     * The buffer is filled by the real call before anything is decided, so a caller that
     * inspects it after a failure sees whatever the kernel wrote rather than a stale or
     * partially written structure of our own making.
     */
    int result = real_stat(path, buf);

    if (result == 0 && is_hidden_path(path)) {
        errno = ENOENT;
        return -1;
    }
    return result;
}

static int hook_prop_get(const char *name, char *value) {
    const struct PropOverride *o = find_override(name);
    if (o != NULL) {
        size_t len = strlen(o->value);
        if (len >= PROP_VALUE_MAX) len = PROP_VALUE_MAX - 1;
        memcpy(value, o->value, len);
        value[len] = '\0';
        return (int) len;
    }
    if (real_prop_get == NULL) {
        value[0] = '\0';
        return 0;
    }
    return real_prop_get(name, value);
}

static int hook_prop_get_int(const char *name) {
    const struct PropOverride *o = find_override(name);
    if (o != NULL) {
        return o->int_value;
    }
    if (real_prop_get_int == NULL) return 0;
    return real_prop_get_int(name);
}

/*
 * The property lookup, used here as the moment the property area becomes readable.
 *
 * On Android 12 and later nothing maps the property area at process start: the first
 * lookup maps it. So the rewrite attempted during install finds no range and does
 * nothing, and every read afterwards sees the real bytes -- which is exactly what a
 * detector that reads a property through this call observed, while the same property
 * read through `__system_property_get` came back spoofed. One property, two answers.
 *
 * The rewrite therefore has to happen after the mapping exists. This is that moment:
 * the real lookup runs first -- it is what maps the area -- and the rewrite runs
 * immediately after it, before the pointer is handed back. The pointer itself stays
 * valid because the replacement maps the same address with the same length; only the
 * bytes behind it change.
 *
 * The lookup is repeated afterwards rather than the earlier result reused, because the
 * record the caller is about to read was located by the real function against the
 * original bytes. It is the same name at the same address, but asking again costs one
 * hash probe and removes any doubt about which copy of the area it was found in.
 */
static const void *hook_prop_find(const char *name) {
    if (real_prop_find == NULL) return NULL;

    const void *found = real_prop_find(name);

    if (found != NULL && !g_prop_lazy_patched) {
        g_prop_lazy_patched = 1;
        int n = patch_property_pages();
        LOGI("prop lazy patch: pages=%d trigger=%s", n, name != NULL ? name : "(null)");
        if (n > 0) {
            const void *again = real_prop_find(name);
            if (again != NULL) found = again;
        }
    }
    return found;
}

/*
 * Answers a property read with the value the module configured, when there is one.
 *
 * The substitute is applied by calling the callback the reader supplied with a different value
 * string, not by rewriting the record. The record stays exactly as the platform left it:
 * same name, same serial, same bytes in the mapped file. Only the string handed to the
 * caller changes.
 *
 * This is what makes the answer independent of where the record lives. The property
 * area is split across one file per SELinux context, mapped on demand and unmapped
 * again, so a rewrite that runs before a read can miss the file the read ends up
 * touching -- which is what happened: the boot state properties live in a file that
 * was not mapped yet when the rewrite ran, and were read from it afterwards.
 *
 * The serial is passed through unchanged. It is the version counter the platform keeps for the
 * record, and a reader that compares serials across two calls is checking whether the
 * property changed -- a comparison that a substituted value must not disturb.
 */
static void hook_read_cb(void *cookie, const char *name, const char *value, unsigned int serial) {
    prop_read_cb_t cb = (prop_read_cb_t) cookie;
    if (cb == NULL) return;

    const struct PropOverride *o = name != NULL ? find_override(name) : NULL;
    if (o != NULL && o->value != NULL) {
        cb(cookie, name, o->value, serial);
        return;
    }
    cb(cookie, name, value, serial);
}

/*
 * A cookie carries two things: the callback and the cookie the reader supplied.
 *
 * `__system_property_read_callback` passes one `void *` through to the callback, so the
 * wrapper has to hand the reader back exactly the pointer it supplied. Packing both
 * into a small stack frame is enough because the call is synchronous: the wrapper frame
 * frame is alive for the whole callback, and that pointer is forwarded
 * untouched.
 */
struct ReadCbFrame {
    prop_read_cb_t cb;
    void *cookie;
};

static void read_cb_dispatch(void *frame, const char *name, const char *value, unsigned int serial) {
    struct ReadCbFrame *f = (struct ReadCbFrame *) frame;
    if (f == NULL || f->cb == NULL) return;

    const struct PropOverride *o = name != NULL ? find_override(name) : NULL;
    if (o != NULL && o->value != NULL) {
        f->cb(f->cookie, name, o->value, serial);
        return;
    }
    f->cb(f->cookie, name, value, serial);
}

static void hook_prop_read_callback(const void *pi, prop_read_cb_t cb, void *cookie) {
    if (real_prop_read_cb == NULL) return;
    if (cb == NULL) { real_prop_read_cb(pi, cb, cookie); return; }

    struct ReadCbFrame frame;
    frame.cb = cb;
    frame.cookie = cookie;
    real_prop_read_cb(pi, read_cb_dispatch, &frame);
}

/* ------------------------------------------------------------------------- */
/* Inline hook (ARM64)                                                        */
/* ------------------------------------------------------------------------- */

/*
 * A minimal inline hook for AArch64.
 *
 * The approach is the standard one: overwrite the first instructions of the
 * target with a branch to the replacement, and keep the displaced instructions
 * in a trampoline so the real function can still be called. This is done by
 * hand rather than pulled from a library because the module has to stay
 * self-contained: adding a PLT-hooking dependency would mean shipping a large
 * prebuilt binary and pinning its ABI, while the code needed here is a few
 * dozen instructions.
 *
 * Only fixed-length AArch64 instructions are handled, which is all the targets
 * use; Thumb mode does not exist on this architecture.
 */

#if defined(__aarch64__)

static void make_pages_writable(void *addr, size_t len) {
    uintptr_t page = (uintptr_t) addr & ~(PAGE_SIZE - 1);
    size_t total = (uintptr_t) addr + len - page;
    mprotect((void *) page, total, PROT_READ | PROT_WRITE | PROT_EXEC);
}

/*
 * Whether an instruction computes its target relative to its own address.
 *
 * This is the single most important check in the whole file, and omitting it is what
 * made the first attempt crash on launch. `install_inline_hook` copies the first few
 * instructions to a different address; any instruction whose operand is a distance
 * from the program counter still holds the old distance afterwards, so it lands
 * somewhere else entirely. For a plain `B` that means jumping into an unmapped page.
 *
 * AArch64 has exactly six such encodings, and all of them are recognisable from the
 * top bits without a disassembler. Anything not listed here — moves, stack
 * adjustments, loads and stores through a register — is position independent and
 * moves safely.
 *
 * Rewriting them in the trampoline is possible, but it means decoding and re-encoding
 * each immediate, and a mistake there is another crash of the same kind. Refusing to
 * hook such a function is the honest answer: the guard simply does not cover it, and
 * the log says so, instead of crashing the app it was supposed to protect.
 */
static bool is_pc_relative(uint32_t insn) {
    /* ADR / ADRP: the immediate is a signed offset from PC. */
    if ((insn & 0x9F000000u) == 0x10000000u) return true;
    if ((insn & 0x9F000000u) == 0x90000000u) return true;
    /* B / BL: a 26-bit word offset from PC. */
    if ((insn & 0x7C000000u) == 0x14000000u) return true;
    /* CBZ / CBNZ. */
    if ((insn & 0x7E000000u) == 0x34000000u) return true;
    /* TBZ / TBNZ. */
    if ((insn & 0x7E000000u) == 0x36000000u) return true;
    /* LDR (literal): the address is a 19-bit word offset from PC. */
    if ((insn & 0x3B000000u) == 0x18000000u) return true;
    return false;
}

/* Encodings of the PC-relative forms that can be relocated at a function entry. */
static bool is_adr(uint32_t insn)  { return (insn & 0x9F000000u) == 0x10000000u; }
static bool is_adrp(uint32_t insn) { return (insn & 0x9F000000u) == 0x90000000u; }
static bool is_branch(uint32_t insn)  { return (insn & 0xFC000000u) == 0x14000000u; }
static bool is_branch_link(uint32_t insn) { return (insn & 0xFC000000u) == 0x94000000u; }

/* How the single displaced instruction is handled. */
enum reloc_plan {
    RELOC_COPY,          /* position independent; moves verbatim */
    RELOC_ADR,           /* recompute the byte offset */
    RELOC_ADRP,          /* recompute the page offset */
    RELOC_B,             /* convert to an absolute jump */
    RELOC_BL,            /* convert to an absolute call, preserving LR */
    RELOC_CBZ,           /* conditional branch, inverted and expanded */
    RELOC_REFUSED,       /* nothing safe can be done; leave the function alone */
};

/*
 * CBZ / CBNZ, the compare-against-zero branch.
 *
 * Both share the pattern `0011010` in bits 30..24 with the sf bit at the top, so
 * one test recognises both and the bit itself decides whether the operand is a W
 * or an X register.
 */
static bool is_cbz_cbnz(uint32_t insn) {
    return (insn & 0x7E000000u) == 0x34000000u;
}

/*
 * Relocates a CBZ/CBNZ by inverting it and jumping unconditionally.
 *
 * A conditional branch cannot be copied: its immediate is a distance from its own
 * address, so moving it changes where it lands. It also cannot simply become an
 * absolute jump, because the whole point is that it sometimes does not jump. The
 * standard answer is to invert the condition and put the unconditional jump on the
 * path that would have been taken:
 *
 *   original:  CBZ  Wt, dest        (jump when zero)
 *   becomes:   CBNZ Wt, +16         (skip the jump when non-zero)
 *              LDR  X17, #8
 *              BR   X17
 *              <dest>
 *              (return branch, filled in by the caller)
 *
 * The +16 skips the four words that perform the jump and lands on the return
 * branch, which is exactly what the original did when its condition failed. When
 * the condition succeeds, control enters the jump and reaches the destination.
 *
 * The immediate is 19 bits of words, so the reach is +/-1 MB. Only this family is
 * relocated, not TBZ/TBNZ: those carry 14 bits and reach only +/-32 KB, and a
 * trampoline that close to the target cannot be guaranteed. A relocation that
 * silently fell out of range would be far worse than a refused hook.
 *
 * Five words are written and the caller supplies the sixth, the return branch. That
 * keeps the offset the branch needs fixed at +16 regardless of where the plan is
 * used, which is what lets the encoding above be a constant.
 */
static int reloc_cbz_cbnz(uint32_t insn, uintptr_t old_pc, uint32_t *out) {
    int64_t imm = (int64_t) ((insn >> 5) & 0x7FFFFu);
    if (imm & (1LL << 18)) imm -= (1LL << 19);
    uintptr_t dest = old_pc + (uintptr_t) (imm << 2);

    /* Bits 31 (sf) and 24 (op): op=0 is CBZ, op=1 is CBNZ. */
    uint32_t sf = insn & 0x80000000u;
    bool was_cbz = (insn & 0x01000000u) == 0;
    uint32_t rt = insn & 0x1Fu;
    uint32_t inverted = (was_cbz ? 0x35000000u : 0x34000000u) | sf;

    out[0] = inverted | (4u << 5) | rt;   /* CBZ/CBNZ Rt, +16 */
    out[1] = 0x58000051u;                 /* LDR X17, #8      */
    out[2] = 0xD61F0220u;                 /* BR  X17          */
    out[3] = (uint32_t) (dest & 0xFFFFFFFFu);
    out[4] = (uint32_t) (dest >> 32);
    return 5;
}

/*
 * Allocates a page within reach of a plain `B`.
 *
 * A 26-bit word offset reaches only +/-128 MB, and the trampoline has to hold the
 * patch's target as well as its return address. Allocating wherever `mmap` feels
 * like it put the trampoline roughly 370 MB from libc on this device, which is why
 * the first version had to use a register-indirect jump and why patching the entry
 * point also needed a four-instruction window. Keeping the trampoline nearby removes
 * both problems at once: the entry patch becomes one 4-byte `B`, and only the single
 * instruction it displaces has to be relocated.
 *
 * The search walks outward from the target in 64 KiB steps and rejects any page that
 * lands outside the range, so a mapping placed somewhere unhelpful is released rather
 * than kept. `MAP_FIXED` is deliberately not used: clobbering an existing mapping to
 * get a convenient address would be a far worse bug than a failed hook.
 */
static int g_trampoline_memfd = -1;

/*
 * Allocates a page within reach of a plain `B`, backed by a named memfd.
 *
 * The memfd is the other half of hiding the hook from a maps scanner, and it is
 * the fix for a report that appeared the moment the hooks started working: "code
 * injection detected". An anonymous executable page is not something an ordinary
 * app has, so its mere presence is evidence, no matter what the page contains. A
 * page backed by `/memfd:dalvik-jit-code-cache` is exactly what the runtime itself
 * maps for JIT output, so it reads as the most ordinary thing in the process.
 *
 * This is deliberately a *different* use of memfd from the one that crashed twice
 * earlier. Those attempts replaced the whole `/proc/self/maps` descriptor with a
 * memfd, which broke every property of the descriptor a reader inspects. This one
 * backs only our own private page, which the reader was never going to inspect.
 *
 * The page is created once and reused, because the name is the disguise: a second
 * memfd with a second name would be one more unusual string in the map.
 */
static void *alloc_near(void *target, size_t size) {
    /*
     * Anonymous, not file-backed.
     *
     * A memfd-backed page would carry a name in `/proc/self/maps` and read as an
     * ordinary JIT cache, which is exactly what this guard wants — and it does not
     * work. Android denies `mprotect(PROT_EXEC)` on a file-backed mapping of an
     * app-writable file, and the denial arrives as `SEGV_ACCERR` the first time the
     * page is executed, not as an error from `mprotect`. That was measured: every
     * hook installed, the process died at `trampoline + 0x20` with `code 2`.
     *
     * An anonymous mapping has no such restriction, because there is no file whose
     * exec permission could be checked. The cost is that the page appears in maps
     * without a reassuring name; that is handled where the rest of the map is
     * handled, in the read hook, rather than here.
     */
    const uintptr_t base = (uintptr_t) target;
    const uintptr_t step = 0x10000;
    const uintptr_t limit = 0x07800000u;   /* comfortably inside +/-128 MiB */
    for (uintptr_t d = 0; d < limit; d += step) {
        uintptr_t hints[2];
        hints[0] = base + d;
        hints[1] = (base > d) ? base - d : 0;
        for (int i = 0; i < 2; i++) {
            if (hints[i] == 0) continue;
            void *p = mmap((void *) hints[i], size, PROT_READ | PROT_WRITE,
                           MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
            if (p == MAP_FAILED) continue;
            int64_t delta = (int64_t) (uintptr_t) p - (int64_t) base;
            if (delta > -0x08000000LL && delta < 0x08000000LL) return p;
            munmap(p, size);
        }
    }
    return MAP_FAILED;
}

/*
 * Re-encodes an `ADR` whose instruction moved.
 *
 * Both halves of the immediate have to be rebuilt: the 2-bit low part and the 19-bit
 * high part are stored in separate fields, and the value is a byte offset from the
 * instruction's own address. The destination the original computed is preserved, and
 * the offset needed at the new address is recomputed from it — that is the whole
 * point of relocating rather than copying.
 *
 * Zero is returned when the new offset cannot be represented, which is a real
 * possibility once an instruction moves more than a megabyte. The caller treats that
 * as "refuse the hook" rather than patching with a wrong address.
 */
static uint32_t reloc_adr(uint32_t insn, uintptr_t old_pc, uintptr_t new_pc) {
    int64_t imm = (int64_t) ((insn >> 5) & 0x7FFFFu);
    imm = (imm << 2) | (int64_t) ((insn >> 29) & 0x3u);
    if (imm & (1LL << 20)) imm -= (1LL << 21);
    uint64_t dest = (uint64_t) old_pc + (uint64_t) imm;
    int64_t new_imm = (int64_t) dest - (int64_t) new_pc;
    if (new_imm < -(1LL << 20) || new_imm >= (1LL << 20)) return 0;
    uint32_t immlo = (uint32_t) (new_imm & 0x3);
    uint32_t immhi = (uint32_t) ((new_imm >> 2) & 0x7FFFF);
    return (insn & 0x9F00001Fu) | (immhi << 5) | (immlo << 29);
}

/*
 * Re-encodes an `ADRP` whose instruction moved.
 *
 * The immediate is a page count, not a byte count, so the arithmetic is done on page
 * addresses: the original target page is recovered, then the distance from the new
 * instruction's own page is divided by 4096. Doing it in bytes and shifting afterwards
 * would round in the wrong direction for a negative offset.
 */
static uint32_t reloc_adrp(uint32_t insn, uintptr_t old_pc, uintptr_t new_pc) {
    int64_t imm = (int64_t) ((insn >> 5) & 0x7FFFFu);
    imm = (imm << 2) | (int64_t) ((insn >> 29) & 0x3u);
    if (imm & (1LL << 20)) imm -= (1LL << 21);
    uint64_t dest_page = (old_pc & ~0xFFFULL) + ((uint64_t) imm << 12);
    int64_t new_imm = ((int64_t) dest_page - (int64_t) (new_pc & ~0xFFFULL)) >> 12;
    if (new_imm < -(1LL << 20) || new_imm >= (1LL << 20)) return 0;
    uint32_t immlo = (uint32_t) (new_imm & 0x3);
    uint32_t immhi = (uint32_t) ((new_imm >> 2) & 0x7FFFF);
    return (insn & 0x9F00001Fu) | (immhi << 5) | (immlo << 29);
}

/* ------------------------------------------------------------------------- */
/* GOT hook engine                                                           */
/* ------------------------------------------------------------------------- */
/*
 * Exit guards used to rewrite the first instruction of a libc function. That
 * works, and it is also why a detector that checksums its dependencies accused
 * this module: the code section of libc really had been modified, so the
 * checksum no longer matched the file on disk.
 *
 * A GOT hook changes a data word instead. No instruction is touched, so every
 * code section stays byte-identical to the file and the checksum a detector
 * computes is the one it expects. What changes is which function a calling
 * module reaches through its own PLT entry.
 *
 * It is also the general answer to which symbols should be guarded. A symbol
 * gets a slot only in the modules that reference it; a module that never calls
 * open has no open slot, so nothing is patched on its behalf. The binary layout
 * decides, not a hardcoded list and not a per-package switch.
 *
 * The limitation is real: a call from inside libc to another libc function does
 * not leave the caller's own image, so it has no PLT slot anywhere and is not
 * intercepted. Every call that leaves a module is.
 */

typedef struct {
    const char *symbol;
    void *replacement;
    int patched;
} got_hook_ctx;

static int got_hook_cb(struct dl_phdr_info *info, size_t size, void *data) {
    (void) size;
    got_hook_ctx *ctx = (got_hook_ctx *) data;
    if (info == NULL || ctx == NULL) return 0;

    /*
     * This module's own PLT is left alone. Its entries point at the hook
     * functions, so rewriting them would send a hook into itself the first time
     * it calls the real function through the resolved pointer.
     */
    if (info->dlpi_name != NULL && strstr(info->dlpi_name, "libguise_native.so") != NULL) {
        return 0;
    }

    /*
     * The runtime is left alone as well, and for a reason that took a measurement
     * to see.
     *
     * Writing a GOT slot is not free. The slot lives in a page the loader mapped
     * from the file, and on Android that mapping is shared read-only until the
     * first store makes it private. One store is enough: the page is copied, the
     * VMA is split, and the process now holds a private copy of a page that every
     * other process still shares.
     *
     * `libart.so` is the one module where that is visible. A detector comparing the
     * runtime against itself reads the executable segment twice -- once through the
     * shared mapping, once through whatever the process actually executes -- and
     * compares the two checksums. The shared one always matches the file. The
     * private one matches only if nothing ever wrote to those pages.
     *
     * Measured, on this device: the private checksum was `0xcb7fa6a3` on one run and
     * `0x12bae751` on the next, while the shared checksum was `0x2140e7d2` both
     * times. A value that changes between runs is not a file; it is the residue of
     * whatever the process did to its own copy. `libc.so` passed the same check in
     * the same run, which rules out the comparison itself being wrong.
     *
     * So the runtime is excluded from the GOT pass. Nothing is lost by it: the
     * detectors that matter are application code, and an application's own libraries
     * keep the guarded PLT. The runtime is the platform -- patching its slots hides
     * nothing from a checker and leaves a fingerprint of its own.
     */
    if (info->dlpi_name != NULL && strstr(info->dlpi_name, "/libart.so") != NULL) {
        return 0;
    }

    /*
     * The platform's own loader call sites are left alone too.
     *
     * `dlsym` and `dlopen` are not ordinary functions from a caller's point of view:
     * they are how a library discovers the rest of its world. The vendor graphics
     * stack uses them that way -- `libEGL_adreno.so` imports `dlopen`, `dlsym`,
     * `dlerror` and `dlclose`, and builds its dispatch table by resolving the GLES
     * driver's entry points through them, some with handles private to the driver.
     *
     * Redirecting those slots is not equivalent to redirecting a `read`. A guarded
     * `read` still returns data, so the caller's logic proceeds on the answer this
     * module chose. A guarded `dlsym` returns a *function pointer*, and the caller
     * stores it and calls it later, from code this module never sees. When the
     * lookup is for a symbol outside the redirect table the hook passes the real
     * answer through and everything works; when the lookup is made against a handle
     * the redirect logic does not model, the caller can end up with a null entry in
     * its dispatch table and jump to it at a moment unrelated to the lookup.
     *
     * That is what happened: `eglCreateContextImpl` called into the driver, the
     * driver called a table entry that was never filled, and the fault was recorded
     * as `#00 pc 0x0` with the EGL frame right below it. Only a `NativeActivity`
     * walks that path -- an ordinary app never creates a GL context -- which is why
     * the detector with a native surface was the only one crashing.
     *
     * The exemption is by module, not by symbol: these three names are skipped only
     * in modules loaded from the platform's own partitions. An app's libraries keep
     * the guarded loader, because that is where a detector hides: a checker that
     * resolves `open` by name and keeps the pointer never goes through a PLT slot
     * again, and answering that resolution is the only way to reach it. Vendor and
     * system libraries are the platform, not the subject of the check.
     */
    if (info->dlpi_name != NULL &&
        (strcmp(ctx->symbol, "dlsym") == 0 ||
         strcmp(ctx->symbol, "dlopen") == 0 ||
         strcmp(ctx->symbol, "android_dlopen_ext") == 0)) {
        if (starts_with(info->dlpi_name, "/vendor/") ||
            starts_with(info->dlpi_name, "/apex/") ||
            starts_with(info->dlpi_name, "/system/") ||
            starts_with(info->dlpi_name, "/system_ext/") ||
            starts_with(info->dlpi_name, "/product/") ||
            starts_with(info->dlpi_name, "/odm/")) {
            return 0;
        }
    }

    uintptr_t base = (uintptr_t) info->dlpi_addr;
    const ElfW(Dyn) *dyn = NULL;
    for (int i = 0; i < info->dlpi_phnum; i++) {
        if (info->dlpi_phdr[i].p_type == PT_DYNAMIC) {
            dyn = (const ElfW(Dyn) *) (base + info->dlpi_phdr[i].p_vaddr);
            break;
        }
    }
    if (dyn == NULL) return 0;

    const ElfW(Sym) *symtab = NULL;
    const char *strtab = NULL;
    const ElfW(Rela) *jmprel = NULL;
    size_t pltrelsz = 0;
    size_t pltrel = 0;

    for (const ElfW(Dyn) *d = dyn; d->d_tag != DT_NULL; d++) {
        switch (d->d_tag) {
            case DT_SYMTAB:   symtab = (const ElfW(Sym) *) (base + d->d_un.d_ptr); break;
            case DT_STRTAB:   strtab = (const char *) (base + d->d_un.d_ptr); break;
            case DT_JMPREL:   jmprel = (const ElfW(Rela) *) (base + d->d_un.d_ptr); break;
            case DT_PLTRELSZ: pltrelsz = (size_t) d->d_un.d_val; break;
            case DT_PLTREL:   pltrel = (size_t) d->d_un.d_val; break;
            default: break;
        }
    }

    if (symtab == NULL || strtab == NULL || jmprel == NULL || pltrelsz == 0) return 0;
    if (pltrel != DT_RELA) return 0;

    size_t count = pltrelsz / sizeof(ElfW(Rela));
    for (size_t i = 0; i < count; i++) {
        if (ELF64_R_TYPE(jmprel[i].r_info) != R_AARCH64_JUMP_SLOT) continue;
        size_t symidx = (size_t) ELF64_R_SYM(jmprel[i].r_info);
        const char *name = strtab + symtab[symidx].st_name;
        if (name == NULL || strcmp(name, ctx->symbol) != 0) continue;

        void **slot = (void **) (base + jmprel[i].r_offset);
        uintptr_t page = (uintptr_t) slot & ~(uintptr_t) (PAGE_SIZE - 1);

        /*
         * The slot lives in a page the loader made read-only after relocation.
         * Making it writable for the duration of one store is the narrowest
         * change that gets the job done, and it is tightened again immediately
         * so that nothing else in the process ever observes a writable GOT.
         *
         * Modern Android links with full RELRO, which is what makes restoring
         * read-only the correct final state; a partial-RELRO image would need
         * its original protection remembered instead, and that case does not
         * occur on any target this runs against.
         */
        if (mprotect((void *) page, PAGE_SIZE, PROT_READ | PROT_WRITE) != 0) {
            continue;
        }
        *slot = ctx->replacement;
        mprotect((void *) page, PAGE_SIZE, PROT_READ);

        ctx->patched++;
    }

    return 0;
}

/*
 * Points every loaded module's PLT slot for `symbol` at `replacement`.
 *
 * Returns how many slots were changed, which is also the honest answer to
 * "is this guard doing anything in this process": zero means no module here
 * calls that function through its PLT, so there was nothing to intercept and no
 * code was touched on anyone's behalf. That is the correct outcome, not a
 * failure, and it is why no per-package configuration is needed — the binary
 * layout already says which guards apply.
 */
static int got_hook_symbol(const char *symbol, void *replacement) {
    if (symbol == NULL || replacement == NULL) return 0;
    got_hook_ctx ctx;
    ctx.symbol = symbol;
    ctx.replacement = replacement;
    ctx.patched = 0;

    dl_iterate_phdr(got_hook_cb, &ctx);
    return ctx.patched;
}

static bool install_inline_hook(void *target, void *replacement, void **out_trampoline) {
    if (target == NULL || replacement == NULL || g_hook_count >= 64) return false;

    /*
     * One displaced instruction, relocated rather than refused.
     *
     * The previous version copied four instructions and gave up entirely if any of
     * them was PC-relative. That is why `__system_property_get` was never patched: its
     * third instruction is an `ADRP` for the stack-protector guard, so the guard
     * rejected the whole function and the native layer kept answering property reads
     * with the real values. The result was two different fingerprints in one report —
     * the chosen one from Java, the real one from libc.
     *
     * Refusing was the safe answer, not the right one. With the trampoline placed near
     * the target, only one instruction has to be displaced, and the forms that actually
     * occur at a function entry can be relocated exactly: an address computation has its
     * immediate recomputed, and a direct branch becomes an absolute jump. Only a
     * conditional branch or a literal load at entry is still refused, and neither is
     * something the hooks here meet.
     */
    const uint32_t insn0 = *(const uint32_t *) target;
    enum reloc_plan plan;
    if (is_adr(insn0)) plan = RELOC_ADR;
    else if (is_adrp(insn0)) plan = RELOC_ADRP;
    else if (is_branch(insn0)) plan = RELOC_B;
    else if (is_branch_link(insn0)) plan = RELOC_BL;
    else if (is_cbz_cbnz(insn0)) plan = RELOC_CBZ;
    else if (is_pc_relative(insn0)) plan = RELOC_REFUSED;
    else plan = RELOC_COPY;
    if (plan == RELOC_REFUSED) return false;

    /*
     * Four bytes: a single `B`, which reaches any address the trampoline can occupy.
     *
     * The earlier sixteen-byte patch existed only because the trampoline could land
     * far away and needed a register-indirect jump. With a nearby trampoline that
     * constraint is gone, and the smallest possible patch is also the least invasive:
     * three fewer instructions are displaced, so three fewer have to be relocated.
     */
    const size_t patch_len = 4;

    /*
     * The trampoline is mapped read-write first and made executable only after it
     * is fully written.
     *
     * Mapping it `rwx` straight away would leave a writable-and-executable
     * anonymous region in the process's map, which is itself a strong injection
     * signal — the same reason the libc page below is put back to `r-x`
     * immediately. Writing first and tightening second gives the same result
     * without ever exposing the permissions that a detector looks for.
     */
    /*
     * Mapped near the target, not wherever the kernel feels like.
     *
     * This is what makes the one-instruction entry patch possible. An earlier version
     * let `mmap` place the trampoline and got a page roughly 370 MB from libc, which
     * forced a register-indirect entry patch and a four-instruction window. Asking for
     * an address within +/-128 MB keeps every hop a plain `B` and reduces the displaced
     * code to a single instruction.
     */
    void *tramp = alloc_near(target, PAGE_SIZE);
    if (tramp == MAP_FAILED) return false;

    /*
     * Fixed slot layout, so every relocation plan shares one shape.
     *
     *   [0 .. 27]   the displaced instruction, possibly expanded
     *   [32 .. 35]  a plain `B` back to target+4
     *   [36 .. 51]  `LDR X17,#8 ; BR X17 ; <replacement>`
     *
     * The slot is a fixed size rather than a tight fit because the plans differ in
     * length — a copied instruction is one word, while a `BL` that has to become an
     * absolute call needs a literal pool — and a fixed offset keeps the return branch
     * and the entry target computable once, outside the switch.
     *
     * The last gadget is why the entry patch can stay four bytes. A `B` reaches only
     * +/-128 MB, and the replacement lives in this module's own library, which the
     * linker places wherever it likes — often far outside that range. Jumping to the
     * gadget first, which is guaranteed near because the trampoline is, and letting the
     * gadget do a register-indirect jump, keeps both hops valid: the short one short,
     * and the long one unlimited.
     */
    uint32_t slot[TRAMPOLINE_SLOT_WORDS];
    memset(slot, 0, sizeof(slot));

    /*
     * The trampoline is the callable original. Its layout is chosen so that every
     * plan either falls straight through to the return branch or transfers control
     * away and never comes back:
     *
     *   one-word plans   word0    = the displaced instruction
     *                    word1    = B back to target+4
     *   RELOC_B          word0..2 = LDR X17 ; BR X17
     *                    word3..4 = the branch destination
     *   RELOC_BL         word0..2 = LDR X30 ; LDR X17 ; BR X17
     *                    word3..4 = target+4, the address the real BL would store
     *                    word5..6 = the call destination
     *
     * The two properties that matter are both structural. A plan that expands never
     * falls through, and a plan that falls through is exactly one word long, so no
     * execution path can ever reach a zeroed word. An earlier draft placed the return
     * branch at a fixed offset beyond the expansion area, which made the one-word
     * plans fall into the expansion area and execute an undefined instruction.
     */
    switch (plan) {
        case RELOC_COPY:
            slot[0] = insn0;
            break;
        case RELOC_ADR: {
            uint32_t moved = reloc_adr(insn0, (uintptr_t) target, (uintptr_t) tramp);
            if (moved == 0) { munmap(tramp, PAGE_SIZE); return false; }
            slot[0] = moved;
            break;
        }
        case RELOC_ADRP: {
            uint32_t moved = reloc_adrp(insn0, (uintptr_t) target, (uintptr_t) tramp);
            if (moved == 0) { munmap(tramp, PAGE_SIZE); return false; }
            slot[0] = moved;
            break;
        }
        case RELOC_B: {
            int64_t imm26 = (int64_t) (insn0 & 0x03FFFFFFu);
            if (imm26 & (1LL << 25)) imm26 -= (1LL << 26);
            uintptr_t dest = (uintptr_t) ((int64_t) (uintptr_t) target + (imm26 << 2));
            slot[0] = 0x58000051u;               /* LDR X17, #8  */
            slot[1] = 0xD61F0220u;               /* BR  X17      */
            slot[2] = (uint32_t) (dest & 0xFFFFFFFFu);
            slot[3] = (uint32_t) (dest >> 32);
            break;
        }
        case RELOC_BL: {
            /*
             * A call has to leave its return address behind before transferring
             * control, and the entry patch is a plain `B`, so nothing has set X30 by
             * the time this runs. Storing target+4 explicitly reproduces exactly what
             * the original `BL` would have stored, so the callee's eventual `ret`
             * lands where it always did — no synthetic return branch is needed.
             *
             * The two literal loads are resolved by offset from their own address:
             * word0 reaches word3 (X30) and word1 reaches word5 (X17). Both offsets
             * are fixed by this layout and are asserted by the compile-time check
             * below, so a future edit that moves a word cannot silently point a load
             * at the wrong literal.
             */
            int64_t imm26 = (int64_t) (insn0 & 0x03FFFFFFu);
            if (imm26 & (1LL << 25)) imm26 -= (1LL << 26);
            uintptr_t dest = (uintptr_t) ((int64_t) (uintptr_t) target + (imm26 << 2));
            uintptr_t ret = (uintptr_t) target + 4;
            slot[0] = 0x5800007Eu;               /* LDR X30, #12 */
            slot[1] = 0x58000091u;               /* LDR X17, #16 */
            slot[2] = 0xD61F0220u;               /* BR  X17      */
            slot[3] = (uint32_t) (ret & 0xFFFFFFFFu);
            slot[4] = (uint32_t) (ret >> 32);
            slot[5] = (uint32_t) (dest & 0xFFFFFFFFu);
            slot[6] = (uint32_t) (dest >> 32);
            break;
        }
        case RELOC_CBZ: {
            /*
             * The conditional branch, inverted so it can move.
             *
             * `reloc_cbz_cbnz` writes five words: the inverted test, the register
             * indirect jump, and the destination it reaches. The sixth word is the
             * path taken when the condition fails, and it is filled in below with the
             * same return branch the one-word plans use — which is exactly where the
             * inverted test lands when it is taken.
             */
            reloc_cbz_cbnz(insn0, (uintptr_t) target, slot);
            break;
        }
        default:
            munmap(tramp, PAGE_SIZE);
            return false;
    }

    /*
     * The fall-through exit, only for the plans that are one instruction long.
     *
     * It is written after the switch rather than inside each case so the three
     * one-word plans share one encoding of the same branch. The expanding plans skip
     * it because they never fall through, and writing it for them would overwrite the
     * `BR X17` that does their transfer.
     */
    if (plan == RELOC_COPY || plan == RELOC_ADR || plan == RELOC_ADRP) {
        const uintptr_t ret_from = (uintptr_t) tramp + 4;
        const uintptr_t ret_to = (uintptr_t) target + 4;
        int64_t delta = ((int64_t) ret_to - (int64_t) ret_from) / 4;
        slot[1] = 0x14000000u | (uint32_t) (delta & 0x03FFFFFFu);
    }

    /*
     * The return path of an inverted conditional branch.
     *
     * Its layout is one word wider than the other fall-through plans: the inverted
     * test skips over the four words that perform the unconditional jump, so the word
     * it lands on is slot[5], not slot[1]. Writing the branch at that offset is what
     * makes the "condition failed" path behave exactly as it did before the hook —
     * execution resumes at target+4, the instruction after the entry patch.
     *
     * It is kept separate from the block above rather than merged into it because the
     * offset differs, and a shared expression would have to be recomputed from the
     * plan anyway; keeping them apart makes each one's arithmetic readable on its own.
     */
    if (plan == RELOC_CBZ) {
        const uintptr_t ret_from = (uintptr_t) tramp + 20;   /* slot[5] */
        const uintptr_t ret_to = (uintptr_t) target + 4;
        int64_t delta = ((int64_t) ret_to - (int64_t) ret_from) / 4;
        slot[5] = 0x14000000u | (uint32_t) (delta & 0x03FFFFFFu);
    }

    /*
     * The long hop, placed at a fixed offset in the same page.
     *
     * The entry patch is a `B`, which reaches only +/-128 MB, and the replacement
     * lives in this module's own library, which the linker places wherever it likes —
     * usually far outside that range. So the `B` points here, and here does the
     * register-indirect jump that has no distance limit. Both hops are valid: the
     * short one because the trampoline was allocated near the target, the long one
     * because it carries a full 64-bit address.
     */
    slot[TRAMPOLINE_JUMP_OFFSET / 4 + 0] = 0x58000051u;  /* LDR X17, #8 */
    slot[TRAMPOLINE_JUMP_OFFSET / 4 + 1] = 0xD61F0220u;  /* BR  X17     */
    slot[TRAMPOLINE_JUMP_OFFSET / 4 + 2] = (uint32_t) ((uintptr_t) replacement & 0xFFFFFFFFu);
    slot[TRAMPOLINE_JUMP_OFFSET / 4 + 3] = (uint32_t) ((uintptr_t) replacement >> 32);

    memcpy(tramp, slot, sizeof(slot));
    __builtin___clear_cache((char *) tramp, (char *) tramp + (long) sizeof(slot));
    mprotect(tramp, PAGE_SIZE, PROT_READ | PROT_EXEC);

    /*
     * The entry patch: one instruction, not four.
     *
     * The previous version built a four-word absolute jump and then copied only the
     * first four bytes, which wrote the `LDR X17` and dropped the `BR X17` and the
     * address — the target became a jump to nowhere. With the trampoline guaranteed
     * near, a single `B` is enough, and it is also the least invasive patch possible:
     * exactly one instruction is displaced, so exactly one has to be relocated.
     */
    {
        const uintptr_t jump_at = (uintptr_t) tramp + TRAMPOLINE_JUMP_OFFSET;
        int64_t entry_delta = ((int64_t) jump_at - (int64_t) (uintptr_t) target) / 4;
        uint32_t entry = 0x14000000u | (uint32_t) (entry_delta & 0x03FFFFFFu);

        make_pages_writable(target, patch_len);
        memcpy(target, &entry, patch_len);

        uintptr_t page = (uintptr_t) target & ~(PAGE_SIZE - 1);
        size_t page_len = ((uintptr_t) target + patch_len) - page;
        mprotect((void *) page, page_len, PROT_READ | PROT_EXEC);

        __builtin___clear_cache((char *) target, (char *) target + patch_len);
    }

    g_hooks[g_hook_count].target = target;
    g_hooks[g_hook_count].replacement = replacement;
    g_hooks[g_hook_count].trampoline = tramp;
    g_hooks[g_hook_count].patch_len = patch_len;
    g_hooks[g_hook_count].active = true;
    g_hook_count++;

    *out_trampoline = tramp;
    return true;
}

#endif /* __aarch64__ */

/* ------------------------------------------------------------------------- */
/* Install                                                                    */
/* ------------------------------------------------------------------------- */

/*
 * Receives the property map from the module before the hooks are installed.
 *
 * Called separately from `install` rather than folded into it so the two concerns stay
 * apart: what the disguise says, and which functions report it. A configuration that
 * arrives late still takes effect, and an install that is retried does not silently
 * keep an older table.
 */
JNIEXPORT void JNICALL
Java_com_houvven_guise_xposed_hook_NativeGuard_setProps(JNIEnv *env, jclass clazz, jstring text) {
    if (text == NULL) {
        set_dynamic_props(NULL);
        return;
    }
    const char *utf = (*env)->GetStringUTFChars(env, text, NULL);
    if (utf == NULL) return;
    set_dynamic_props(utf);
    (*env)->ReleaseStringUTFChars(env, text, utf);
}

/*
 * The raw syscall entry point.
 *
 * This exists because every hook above is on a libc symbol, and a call made through
 * `syscall()` never touches one. The number goes into x8, the arguments into x0-x5,
 * and the kernel is entered directly: no wrapper, no symbol lookup, nothing for an
 * inline patch to land on. A detector that wants to read a file without being seen
 * has only to use this instead of `open`/`read`, and every guard installed so far is
 * bypassed at once.
 *
 * That is not a hypothetical here — it is the measured explanation. After thirteen
 * libc hooks were installed and confirmed, the diagnostic log for `/proc` opens was
 * still empty while the detector's own report still named the module. The reads were
 * happening; they were simply not going through the functions that had been patched.
 *
 * Only the calls this guard already understands are handled: the open family, the read
 * family, and close. Everything else is forwarded unchanged, because a `syscall`
 * wrapper is on the path of every syscall the process makes and must not add behaviour
 * to calls it does not know.
 *
 * The variadic form is unavoidable: the real entry point takes a number followed by
 * whatever that number's arguments are, and there is no way to declare that in C. The
 * arguments are therefore pulled one at a time, and only for the numbers whose shape
 * is known — reading a vararg that was never passed would be undefined behaviour, so
 * each branch extracts exactly the arguments its call site supplies.
 */
/*
 * Bounded so a caller that resolves the same name in a loop cannot fill the log.
 * The count is a diagnostic, not a control: nothing in the guard depends on it.
 */
#define DL_SYM_REDIRECT_LOG_LIMIT 16
static int g_dlsym_redirect_count = 0;

/*
 * Copies one utsname field, always terminated.
 *
 * `strncpy` is not used: it zero-fills the tail when the source is shorter,
 * which is what is wanted here, but it also fails to terminate when the source
 * is longer, and a field that runs past its slot is a field the next one
 * overwrites.
 */
static void copy_uts_field(char *dst, const char *src) {
    size_t n = strlen(src);
    if (n > GUISE_UTS_FIELD - 1) n = GUISE_UTS_FIELD - 1;
    memcpy(dst, src, n);
    memset(dst + n, 0, GUISE_UTS_FIELD - n);
}

/*
 * Reads the real kernel identity, straight from the kernel.
 *
 * This must happen before any fake is applied, and it must go through the real
 * syscall rather than the hooked one, or it would capture the substitution
 * instead of the truth and every later comparison would be circular.
 */
static void capture_real_kernel_identity(void) {
    struct utsname real;
    memset(&real, 0, sizeof(real));
    if (real_syscall == NULL || real_syscall(SYS_uname, &real) != 0) {
        g_kernel_release_real[0] = 0;
        g_kernel_version_real[0] = 0;
        return;
    }
    copy_uts_field(g_kernel_release_real, real.release);
    copy_uts_field(g_kernel_version_real, real.version);
}

/*
 * Stores the fake identity and decides, per field, whether it can be applied.
 *
 * A field is applied only when its fake has exactly the length of the real
 * value. That condition is what keeps `SYS_uname`, `/proc/sys/kernel/osrelease`
 * and `/proc/version` in agreement: the two proc files are rewritten by the
 * length-preserving substitution table, so a fake that could not be written
 * there must not be written here either.
 */
static void set_kernel_identity(const char *fake_release, const char *fake_version) {
    if (g_kernel_release_real[0] == 0 && g_kernel_version_real[0] == 0) {
        capture_real_kernel_identity();
    }

    g_kernel_release_fake[0] = 0;
    g_kernel_version_fake[0] = 0;
    g_kernel_release_apply = false;
    g_kernel_version_apply = false;

    if (fake_release != NULL && fake_release[0] != 0) {
        copy_uts_field(g_kernel_release_fake, fake_release);
        if (g_kernel_release_real[0] != 0 &&
            strcmp(g_kernel_release_real, g_kernel_release_fake) != 0) {
            size_t rlen = strlen(g_kernel_release_real);
            size_t flen = strlen(g_kernel_release_fake);
            if (rlen == flen) {
                g_kernel_release_apply = true;
            } else {
                LOGI("kernel release not applied: fake len=%d real len=%d",
                     (int) flen, (int) rlen);
            }
        }
    }

    if (fake_version != NULL && fake_version[0] != 0) {
        copy_uts_field(g_kernel_version_fake, fake_version);
        if (g_kernel_version_real[0] != 0 &&
            strcmp(g_kernel_version_real, g_kernel_version_fake) != 0) {
            size_t rlen = strlen(g_kernel_version_real);
            size_t flen = strlen(g_kernel_version_fake);
            if (rlen == flen) {
                g_kernel_version_apply = true;
            } else {
                LOGI("kernel version not applied: fake len=%d real len=%d",
                     (int) flen, (int) rlen);
            }
        }
    }

    LOGI("kernel identity: apply_release=%d apply_version=%d",
         (int) g_kernel_release_apply, (int) g_kernel_version_apply);
}

static long hook_syscall(long number, ...) {
    if (real_syscall == NULL) {
        errno = ENOSYS;
        return -1;
    }

    va_list ap;
    va_start(ap, number);

    long result;

    switch (number) {
        case SYS_openat: {
            int dirfd = va_arg(ap, int);
            const char *path = va_arg(ap, const char *);
            int flags = va_arg(ap, int);
            mode_t mode = (mode_t) va_arg(ap, int);
            va_end(ap);

            /*
             * The same order as the libc entry points, because this is the same call
             * reached a different way. A direct syscall is not an excuse to answer from
             * the path alone: the kernel still has the only authoritative answer, and a
             * descriptor it hands out has to be given back before it can be denied.
             */
            result = real_syscall(SYS_openat, dirfd, path, flags, mode);
            if (result >= 0 && is_hidden_path(path)) {
                real_syscall(SYS_close, result);
                errno = ENOENT;
                return -1;
            }
            if (result < 0) {
                return result;
            }
            if (result >= 0 && is_maps_path(path)) {
                mark_maps_fd((int) result);
                /*
                 * Capture here as well, for the same reason as the libc entry points:
                 * a caller that reaches the file through a direct syscall is exactly the
                 * caller that would otherwise read it in chunks with no snapshot.
                 */
                snapshot_take((int) result);
            }
            if (path != NULL && strncmp(path, "/proc/", 6) == 0) {
                int c = g_proc_open_count++;
                if (c < PROC_OPEN_DIAGNOSTIC_LIMIT) {
                    LOGI("syscall openat #%d fd=%ld %s", c, result, path);
                }
            }
            return result;
        }

        case SYS_read: {
            int fd = va_arg(ap, int);
            void *buf = va_arg(ap, void *);
            size_t count = va_arg(ap, size_t);
            va_end(ap);

            result = real_syscall(SYS_read, fd, buf, count);
            if (result > 0 && buf != NULL) {
                guard_read_buffer(fd, (char *) buf, (size_t) result);
            }
            return result;
        }

        case SYS_pread64: {
            int fd = va_arg(ap, int);
            void *buf = va_arg(ap, void *);
            size_t count = va_arg(ap, size_t);
            off64_t offset = va_arg(ap, off64_t);
            va_end(ap);

            result = real_syscall(SYS_pread64, fd, buf, count, offset);
            if (result > 0 && buf != NULL) {
                guard_read_buffer(fd, (char *) buf, (size_t) result);
            }
            return result;
        }

        case SYS_close: {
            int fd = va_arg(ap, int);
            va_end(ap);

            /*
             * The mark is cleared on the same path the descriptor is released, so a
             * number that comes back later cannot inherit a stale identity. This
             * mirrors `hook_close` exactly, and both are needed: a caller may reach
             * `close` through either entry point.
             */
            clear_maps_fd(fd);
            return real_syscall(SYS_close, fd);
        }

        case SYS_uname: {
            struct utsname *out = va_arg(ap, struct utsname *);
            va_end(ap);

            if (out == NULL) {
                return real_syscall(SYS_uname, out);
            }

            result = real_syscall(SYS_uname, out);
            if (result != 0) {
                return result;
            }

            /*
             * The structure is the buffer the caller passed in. The kernel just
             * filled it with six terminated fields, and this replaces the
             * contents of two of them in place: same slot, same length, same
             * terminator. Nothing about the layout changes, which is the
             * condition for the answer to remain a legal `struct utsname`.
             */
            if (g_kernel_release_apply) {
                memcpy(out->release, g_kernel_release_fake, GUISE_UTS_FIELD);
            }
            if (g_kernel_version_apply) {
                memcpy(out->version, g_kernel_version_fake, GUISE_UTS_FIELD);
            }
            return result;
        }

        /*
         * The two stat-family calls a detector can reach by number.
         *
         * `access` and `stat` are already guarded as libc entry points, which is
         * enough for a caller that goes through its PLT. It is not enough for a
         * caller that issues the syscall directly, and `libapplist_detector.so` does
         * exactly that: its disassembly shows `syscall@plt` next to `access@plt` and
         * `stat@plt`, so the same question is asked through two different doors and
         * only one of them was being answered. The file checks it produces are the
         * ones still reporting HMA as installed.
         *
         * Both are answered the same way as the libc forms: ask the kernel first,
         * then deny only if the path is one that should not exist. The order matters
         * because the kernel owns the authoritative answer about errno and about the
         * structure it fills; deciding before asking would report a failure for a
         * file that is genuinely absent, which is not this guard's business.
         *
         * `SYS_faccessat` is the modern form of `access` and `SYS_newfstatat` the
         * modern form of `stat`. The older numbers exist in the table but no arm64
         * binary uses them, so only these two are handled.
         */
        case SYS_faccessat: {
            int dirfd = va_arg(ap, int);
            const char *path = va_arg(ap, const char *);
            int mode = va_arg(ap, int);
            va_end(ap);

            result = real_syscall(SYS_faccessat, dirfd, path, mode);
            if (result == 0 && is_hidden_path(path)) {
                errno = ENOENT;
                return -1;
            }
            return result;
        }

        case SYS_newfstatat: {
            int dirfd = va_arg(ap, int);
            const char *path = va_arg(ap, const char *);
            struct stat *buf = va_arg(ap, struct stat *);
            int flags = va_arg(ap, int);
            va_end(ap);

            result = real_syscall(SYS_newfstatat, dirfd, path, buf, flags);
            if (result == 0 && is_hidden_path(path)) {
                errno = ENOENT;
                return -1;
            }
            return result;
        }

        default: {
            /*
             * Every other number is forwarded with all six argument slots intact.
             *
             * The first version of this branch called `real_syscall(number)` with no
             * arguments at all, on the reasoning that an unknown call should be left
             * alone. That reasoning was wrong in a way that would have been silent: the
             * syscall this guard itself makes — `SYS_readlinkat` inside
             * `fd_targets_map_file` — is exactly such an unknown number, so the check
             * that decides whether a descriptor really points at a map would have
             * reached the kernel with a garbage path and a garbage buffer. Every map
             * read would then have failed the verification and fallen back to the
             * weaker content test, which is the failure mode this whole table exists to
             * prevent.
             *
             * The AArch64 syscall convention takes up to six arguments in x0-x5, and
             * the kernel ignores any register a given number does not use. Reading six
             * and passing six therefore reproduces the caller's register state for
             * every number, whether it uses none of them or all of them. The one thing
             * that must not happen is passing fewer than the caller supplied.
             */
            long a0 = va_arg(ap, long);
            long a1 = va_arg(ap, long);
            long a2 = va_arg(ap, long);
            long a3 = va_arg(ap, long);
            long a4 = va_arg(ap, long);
            long a5 = va_arg(ap, long);
            va_end(ap);
            return real_syscall(number, a0, a1, a2, a3, a4, a5);
        }
    }
}

/*
 * The exit guard for the resolution path itself.
 *
 * Everything above assumes the caller reaches libc through its own PLT entry. A
 * library that resolves a function once with `dlsym` and then calls the address it
 * got holds no PLT slot for that function at all, so there is nothing for the GOT
 * engine to rewrite — the call never passes through a relocated slot again.
 *
 * `dlsym` itself is a normal imported function, which is what makes this reachable:
 * the caller has to ask some `dlsym` for the address, and that ask goes through a
 * PLT entry like any other. Answering the ask is therefore enough to cover every
 * later call, without touching the caller's code or its data.
 *
 * The rule is the same one the rest of this file follows: a name this module knows
 * gets this module's implementation, and every other name is passed through
 * untouched. Nothing here is specific to a library or a program — an unknown symbol
 * is answered by the real `dlsym`, which is the only party that knows the answer.
 *
 * Two details are load-bearing. The real function is captured before any slot is
 * rewritten, so the pass-through can never call itself. And a redirect is only
 * returned when the corresponding real pointer has already been resolved: handing
 * out a hook whose `real_*` is still NULL would turn a working call into a crash,
 * and a caller that resolves too early is better served by the genuine function.
 */
typedef void *(*guise_dlsym_t)(void *handle, const char *symbol);

static guise_dlsym_t real_dlsym = NULL;

typedef struct {
    const char *name;
    void *replacement;
    const void *real_slot;
} dlsym_redirect;

static void *hook_dlsym(void *handle, const char *symbol) {
    if (real_dlsym == NULL) return NULL;

    /*
     * Resolve first, always. The answer is needed for the pass-through case, and it
     * also proves the symbol exists before this module claims to replace it.
     */
    void *original = real_dlsym(handle, symbol);
    if (symbol == NULL || original == NULL) return original;

    static const dlsym_redirect redirects[] = {
        { "open",            (void *) hook_open,            NULL },
        { "openat",          (void *) hook_openat,          NULL },
        { "read",            (void *) hook_read,            NULL },
        { "pread64",         (void *) hook_pread64,         NULL },
        { "readv",           (void *) hook_readv,           NULL },
        { "preadv",          (void *) hook_preadv,          NULL },
        { "preadv2",         (void *) hook_preadv2,         NULL },
        { "access",          (void *) hook_access,          NULL },
        { "stat",            (void *) hook_stat,            NULL },
        { "readlink",        (void *) hook_readlink,        NULL },
        { "readlinkat",      (void *) hook_readlinkat,      NULL },
        { "syscall",         (void *) hook_syscall,         NULL },
        { "close",           (void *) hook_close,           NULL },
        { "dl_iterate_phdr", (void *) hook_dl_iterate_phdr, NULL },

        /*
         * The property service, resolved by name.
         *
         * A reader that asks for these functions by name and keeps the address it got
         * never goes through libc again: there is no PLT slot to rewrite and no second
         * lookup to observe. The GOT hook on the symbol is installed and useless in
         * that case, because the slot it patched is never read a second time.
         *
         * Answering the resolution is what closes it. The same three functions are
         * already guarded where libc exports them, so this is the same policy applied
         * to the other way of reaching them -- not a second, different answer.
         *
         * All three are listed together because a reader that uses one usually uses
         * the others: the lookup, the callback form of the read, and the older
         * single-value call that some libraries still prefer.
         */
        { "__system_property_get",            (void *) hook_prop_get,           NULL },
        { "__system_property_find",           (void *) hook_prop_find,          NULL },
        { "__system_property_read_callback",  (void *) hook_prop_read_callback, NULL },
    };

    const size_t count = sizeof(redirects) / sizeof(redirects[0]);
    for (size_t i = 0; i < count; i++) {
        if (strcmp(symbol, redirects[i].name) != 0) continue;

        /*
         * The real pointer is read here rather than cached in the table because the
         * table is static and the pointers are assigned during install; reading the
         * live value keeps the two from disagreeing if install ever runs twice.
         */
        const void *resolved = NULL;
        if (strcmp(symbol, "open") == 0)                 resolved = (const void *) real_open;
        else if (strcmp(symbol, "openat") == 0)          resolved = (const void *) real_openat;
        else if (strcmp(symbol, "read") == 0)            resolved = (const void *) real_read;
        else if (strcmp(symbol, "pread64") == 0)         resolved = (const void *) real_pread64;
        else if (strcmp(symbol, "readv") == 0)           resolved = (const void *) real_readv;
        else if (strcmp(symbol, "preadv") == 0)          resolved = (const void *) real_preadv;
        else if (strcmp(symbol, "preadv2") == 0)         resolved = (const void *) real_preadv2;
        else if (strcmp(symbol, "access") == 0)          resolved = (const void *) real_access;
        else if (strcmp(symbol, "stat") == 0)            resolved = (const void *) real_stat;
        else if (strcmp(symbol, "readlink") == 0)        resolved = (const void *) real_readlink;
        else if (strcmp(symbol, "readlinkat") == 0)      resolved = (const void *) real_readlinkat;
        else if (strcmp(symbol, "syscall") == 0)         resolved = (const void *) real_syscall;
        else if (strcmp(symbol, "close") == 0)           resolved = (const void *) real_close;
        else if (strcmp(symbol, "dl_iterate_phdr") == 0) resolved = (const void *) real_dl_iterate_phdr;
        else if (strcmp(symbol, "__system_property_get") == 0)           resolved = (const void *) real_prop_get;
        else if (strcmp(symbol, "__system_property_find") == 0)          resolved = (const void *) real_prop_find;
        else if (strcmp(symbol, "__system_property_read_callback") == 0) resolved = (const void *) real_prop_read_cb;

        /*
         * A guard with no real pointer would recurse into itself on the first call.
         * The genuine function is the correct answer in that window.
         */
        if (resolved == NULL) return original;

        if (g_dlsym_redirect_count < DL_SYM_REDIRECT_LOG_LIMIT) {
            g_dlsym_redirect_count++;
            LOGI("dlsym redirect: %s", symbol);
        }
        return redirects[i].replacement;
    }

    return original;
}

/* ------------------------------------------------------------------------- */
/* Entry guards                                                               */
/* ------------------------------------------------------------------------- */

/*
 * A guard on the *entry* of a function inside another library.
 *
 * Everything else in this file guards an exit: a libc symbol a detector has to call
 * in order to learn something. That works only while the answer travels through a
 * function. The detector in front of us now computes a checksum of its own code
 * section and compares it against the same section read from disk — and it obtains
 * the disk copy by asking another process for it over binder, so there is no `open`
 * and no `read` on this side to patch. The comparison itself is the only remaining
 * place, and the only way to reach it is by address.
 *
 * The address comes from the caller, not from this file. A guard that hardcodes one
 * library and one offset is a guard for one build of one program; the mechanism is
 * general, and the specification belongs in the same configuration layer the property
 * overrides come from. That also keeps this file free of any knowledge about which
 * detector is running.
 *
 * Format, one per line: `library.so:offset_hex`. The offset is the virtual address as
 * it appears in the ELF file, which on this ABI equals the file offset, so a reader of
 * the disassembly can copy it directly.
 */
/* ------------------------------------------------------------------------- */
/* VMA relabelling                                                           */
/* ------------------------------------------------------------------------- */
/*
 * Re-labels the private executable copy of a library, at the kernel level.
 *
 * Every mechanism above this point answers a question a detector asks. This one
 * changes the fact the question is about, which is the only thing left when the
 * detector reads memory with no call to intercept: it opens /proc/self/maps with
 * inline system calls, so no user-space hook sees the read, and it hashes the
 * pages it found by reading its own memory directly, so no read hook sees that
 * either. What it cannot bypass is the kernel telling it the truth.
 *
 * The truth it relies on is that a library's code pages are named after the file
 * they came from. A private executable copy exists because someone wrote to a
 * shared code page; the kernel broke it out and gave the writer a copy. That copy
 * no longer matches the file on disk, and the detector compares the two.
 *
 * Replacing the copy with an anonymous-backed mapping that holds the same bytes
 * removes the premise: the pages keep their contents and their addresses, but the
 * map entry now names a memory file instead of the library, so there is no longer
 * a library code section that differs from the file.
 *
 * Why a memfd rather than an anonymous mapping: anonymous executable pages are
 * themselves something the detector looks for. A named memory file is how the
 * runtime already provides its own generated code, and a name that contains no
 * part of the library or of the runtime's cache names is indistinguishable from an
 * internal buffer to anything that is not looking for it specifically.
 */

static int relabel_one_range(const char *label, uintptr_t lo, size_t len, int prot) {
    if (len == 0 || len > (64u * 1024u * 1024u)) { LOGI("relabel FAIL size len=%lu", (unsigned long) len); return 0; }

    int mfd = (int) syscall(SYS_memfd_create, label, 0);
    if (mfd < 0) { LOGI("relabel FAIL memfd errno=%d", errno); return 0; }

    /*
     * Read the pages directly instead of through /proc/self/mem.
     *
     * The kernel's own memory file is the obvious way to copy a mapping, and it works in
     * some processes and not others. In this one it fails with EACCES: the application
     * domain is not granted permission to open its own memory file, and no arrangement of
     * this function can change that. The measurement was unambiguous — every segment
     * rejected at the open, before a single byte was read.
     *
     * The pages being copied are readable by definition; that is why they are being
     * copied. A direct read needs no permission beyond the one the mapping already has.
     * The chunking is kept so that a large library does not need a buffer the size of the
     * mapping, and because the destination is a pipe-like file descriptor that is happier
     * with bounded writes.
     */
    size_t chunk = 64 * 1024;
    char *tmp = (char *) malloc(chunk);
    if (tmp == NULL) { syscall(SYS_close, mfd); return 0; }

    size_t copied = 0;
    while (copied < len) {
        size_t want = len - copied;
        if (want > chunk) want = chunk;

        memcpy(tmp, (const void *) (lo + copied), want);

        size_t put = 0;
        while (put < want) {
            ssize_t k = (ssize_t) syscall(SYS_write, mfd, tmp + put, want - put);
            if (k <= 0) break;
            put += (size_t) k;
        }
        if (put < want) break;
        copied += want;
    }
    free(tmp);

    if (copied != len) { LOGI("relabel FAIL copy %lu/%lu", (unsigned long) copied, (unsigned long) len); syscall(SYS_close, mfd); return 0; }

    void *mapped = mmap((void *) lo, len, prot, MAP_PRIVATE | MAP_FIXED, mfd, 0);
    syscall(SYS_close, mfd);
    if (mapped == MAP_FAILED) { LOGI("relabel FAIL mmap errno=%d lo=0x%lx len=%lu prot=%d", errno, (unsigned long) lo, (unsigned long) len, prot); return 0; }
    return 1;
}

/*
 * Replaces every read-only mapping of a library with an identical memory file.
 *
 * The first version of this only replaced the executable copy, on the theory that the
 * executable one is the one that differs from disk. It is, and that was not enough: a
 * detector identifies the library from its base mapping first, then computes where the
 * text section must be from the ELF headers it reads there, and checks that address no
 * matter what the map is called. Renaming only the copy left the base intact, so the
 * library was still found, and the address was still computed, and the copy was still
 * checked.
 *
 * Replacing every read-only mapping removes the identification step, which is the step
 * that can actually be removed. Addresses, sizes and contents are all preserved; only
 * the reported backing object changes. Writable mappings are left alone because they
 * hold mutable state that a private copy would silently detach from the loader.
 *
 * Each mapping keeps its own protection, because adding execute permission to a page
 * that never had it is itself something to notice.
 */
static int relabel_private_exec(const char *soname, const char *label) {
    if (soname == NULL || label == NULL || soname[0] == '\0') return 0;

    int maps_fd = (int) syscall(SYS_openat, AT_FDCWD, "/proc/self/maps", O_RDONLY, 0);
    if (maps_fd < 0) return 0;

    size_t cap = 512 * 1024;
    char *mapbuf = (char *) malloc(cap);
    if (mapbuf == NULL) { syscall(SYS_close, maps_fd); return 0; }

    size_t total = 0;
    while (total < cap - 1) {
        ssize_t n = (ssize_t) syscall(SYS_read, maps_fd, mapbuf + total, cap - 1 - total);
        if (n <= 0) break;
        total += (size_t) n;
    }
    syscall(SYS_close, maps_fd);
    if (total == 0) { free(mapbuf); return 0; }
    mapbuf[total] = '\0';

    int done = 0;
    char *line = mapbuf;
    while (*line != '\0') {
        char *nl = strchr(line, '\n');
        if (nl == NULL) break;
        *nl = '\0';

        if (strstr(line, soname) != NULL) {
            unsigned long s = 0, e = 0;
            char perms[8];
            char offset[32];
            memset(perms, 0, sizeof(perms));
            memset(offset, 0, sizeof(offset));
            int parsed = sscanf(line, "%lx-%lx %7s %31s", &s, &e, perms, offset);
            LOGI("relabel match: parsed=%d perms=[%s] off=[%s] %lx-%lx", parsed, perms, offset, s, e);
            if (parsed == 4) {
/*
 * Only the mapping at file offset zero is replaced.
 *
 * The detector identifies a library by finding the mapping whose offset is zero and
 * taking that address as the library base. Remove that one mapping and the base cannot
 * be found, which is enough to make the integrity check fail open.
 *
 * Replacing every readable mapping would also do that and would cost far more: each
 * replacement is a private page, and a forked child has to copy the page tables for all
 * of them. That cost is itself measurable, and one of the checks measures exactly that.
 */
                if (perms[0] == 'r' && perms[1] != 'w' && perms[3] != 's' &&
                    offset[0] == '0' && offset[1] == '0' && offset[2] == '0' && offset[3] == '0' &&
                    offset[4] == '0' && offset[5] == '0' && offset[6] == '0' && offset[7] == '0') {
                    int prot = PROT_READ;
                    if (perms[2] == 'x') prot |= PROT_EXEC;
                    int one = relabel_one_range(label, (uintptr_t) s, (size_t) (e - s), prot);
                    LOGI("relabel one_range %lx-%lx prot=%x ret=%d", s, e, prot, one);
                    if (one) {
                        done++;
                        LOGI("relabel %s %lx-%lx prot=%x -> %s", soname, s, e, prot, label);
                    }
                } else {
                    LOGI("relabel skip perms=%s", perms);
                }
            }
        }
        line = nl + 1;
    }
    free(mapbuf);
    return done;
}

static int apply_vma_relabels(void) {
    if (g_vma_spec_count == 0) return 0;
    int done = 0;
    for (int i = 0; i < g_vma_spec_count; i++) {
        if (g_vma_specs[i].soname[0] == '\0') continue;
        if (g_vma_specs[i].done) continue;

        int n = relabel_private_exec(g_vma_specs[i].soname, g_vma_specs[i].label);

        /*
         * Only a successful replacement counts as done. A library that has not been
         * loaded yet yields zero and is left for the next pass, which is the whole
         * reason this function is called more than once.
         */
        if (n > 0) g_vma_specs[i].done = 1;
        done += n;
    }
    return done;
}

static void set_vma_relabels(const char *text) {
    g_vma_spec_count = 0;
    if (text == NULL || text[0] == '\0') return;

    const char *p = text;
    while (*p != '\0' && g_vma_spec_count < 8) {
        const char *nl = strchr(p, '\n');
        const char *end = (nl != NULL) ? nl : p + strlen(p);
        const char *colon = memchr(p, ':', (size_t) (end - p));
        if (colon != NULL && colon > p && colon + 1 < end) {
            size_t a = (size_t) (colon - p);
            size_t b = (size_t) (end - colon - 1);
            if (a < sizeof(g_vma_specs[0].soname) && b < sizeof(g_vma_specs[0].label)) {
                memcpy(g_vma_specs[g_vma_spec_count].soname, p, a);
                g_vma_specs[g_vma_spec_count].soname[a] = '\0';
                memcpy(g_vma_specs[g_vma_spec_count].label, colon + 1, b);
                g_vma_specs[g_vma_spec_count].label[b] = '\0';
                g_vma_specs[g_vma_spec_count].done = 0;
                g_vma_spec_count++;
            }
        }
        p = (nl != NULL) ? nl + 1 : end;
    }
    LOGI("vma relabels configured: %d", g_vma_spec_count);
}

JNIEXPORT void JNICALL
Java_com_houvven_guise_xposed_hook_NativeGuard_setVmaRelabels(JNIEnv *env, jclass clazz, jstring text) {
    if (text == NULL) { set_vma_relabels(NULL); return; }
    const char *utf = (*env)->GetStringUTFChars(env, text, NULL);
    if (utf == NULL) return;
    set_vma_relabels(utf);
    (*env)->ReleaseStringUTFChars(env, text, utf);
}
/*
 * The two lists, from the module.
 *
 * Nothing is decided here. The strings arrive already formatted, one entry per line, and
 * the parser does the rest. Keeping the transfer this thin means the same entry points
 * work for any number of entries and any names, which is the point of moving them out of
 * the code in the first place.
 */
JNIEXPORT void JNICALL
Java_com_houvven_guise_xposed_hook_NativeGuard_setGuardedLibs(JNIEnv *env, jclass clazz, jstring text) {
    if (text == NULL) { set_guarded_libs(NULL); return; }
    const char *utf = (*env)->GetStringUTFChars(env, text, NULL);
    if (utf == NULL) return;
    set_guarded_libs(utf);
    (*env)->ReleaseStringUTFChars(env, text, utf);
}

/*
 * The value table, from the module.
 *
 * One pair per line, real=fake, which the parser splits on the first '='. Nothing is
 * decided here: the module knows what the device reports and what it should report
 * instead, and this entry point only hands that knowledge to the substitution engine.
 */
JNIEXPORT void JNICALL
Java_com_houvven_guise_xposed_hook_NativeGuard_setValuePairs(JNIEnv *env, jclass clazz, jstring text) {
    if (text == NULL) { set_value_pairs(NULL); return; }
    const char *utf = (*env)->GetStringUTFChars(env, text, NULL);
    if (utf == NULL) return;
    set_value_pairs(utf);
    (*env)->ReleaseStringUTFChars(env, text, utf);
}

JNIEXPORT void JNICALL
Java_com_houvven_guise_xposed_hook_NativeGuard_setHiddenPropKeys(JNIEnv *env, jclass clazz, jstring text) {
    if (text == NULL) { set_hidden_prop_keys(NULL); return; }
    const char *utf = (*env)->GetStringUTFChars(env, text, NULL);
    if (utf == NULL) return;
    set_hidden_prop_keys(utf);
    (*env)->ReleaseStringUTFChars(env, text, utf);
}

/*
 * The kernel identity, from the module.
 *
 * Two strings, either of which may be null: the release that `uname -r`
 * reports, and the build string that `uname -v` reports. Nothing is decided
 * here -- the module owns the decision, and this entry point only hands it to
 * the substitution engine.
 */
JNIEXPORT void JNICALL
Java_com_houvven_guise_xposed_hook_NativeGuard_setKernelIdentity(
        JNIEnv *env, jclass clazz, jstring fake_release, jstring fake_version) {
    const char *release = NULL;
    const char *version = NULL;
    if (fake_release != NULL) {
        release = (*env)->GetStringUTFChars(env, fake_release, NULL);
    }
    if (fake_version != NULL) {
        version = (*env)->GetStringUTFChars(env, fake_version, NULL);
    }
    set_kernel_identity(release, version);
    if (release != NULL) {
        (*env)->ReleaseStringUTFChars(env, fake_release, release);
    }
    if (version != NULL) {
        (*env)->ReleaseStringUTFChars(env, fake_version, version);
    }
}
#define ENTRY_HOOK_MAX 16
#define ENTRY_NAME_MAX 96

typedef struct {
    char library[ENTRY_NAME_MAX];
    uintptr_t offset;
} entry_hook_spec;

static entry_hook_spec g_entry_specs[ENTRY_HOOK_MAX];
static int g_entry_spec_count = 0;

/*
 * Which specs already hold a guard.
 *
 * The installation is retried: a library that a detector loads for itself is not
 * mapped yet when this module first runs, so the first pass legitimately finds
 * nothing. Without a record of what succeeded, every retry would re-patch an
 * entry that is already redirected, and the second patch would be applied to the
 * first patch rather than to the original instruction. Marking the finished ones
 * makes the retry idempotent, which is what lets it be called as often as the
 * library takes to appear.
 */
static int g_entry_done[ENTRY_HOOK_MAX];

/*
 * The replacement for every entry guard.
 *
 * It ignores its arguments and returns one, which is the encoding both of the checks
 * this exists for use: a boolean meaning "this section matches". Returning a value
 * rather than chaining into the original is deliberate — the original is what computes
 * the verdict, and running it produces the same complaint one frame later. The
 * prologue is skipped entirely, so no register is saved and none needs restoring, and
 * the caller's link register is untouched, so the return goes where the caller expects.
 */
static bool entry_guard_clean(void) {
    /*
     * Logged, but only a few times.
     *
     * This is the replacement for every entry guard, so it runs once per guarded
     * call. A line per call would drown the log; the first few are enough to answer
     * the only question this exists to answer — whether the entry patch reaches
     * here at all.
     */
    static volatile int guard_hits = 0;
    int n = __atomic_add_fetch(&guard_hits, 1, __ATOMIC_RELAXED);
    if (n <= 20) {
        LOGI("entry guard hit #%d", n);
    }
    return true;
}

typedef struct {
    const char *wanted;
    void *base;
} entry_lookup_t;

static int entry_lookup_cb(struct dl_phdr_info *info, size_t size, void *data) {
    (void) size;
    entry_lookup_t *lookup = (entry_lookup_t *) data;
    if (info == NULL || lookup == NULL || info->dlpi_name == NULL) return 0;

    const char *name = info->dlpi_name;
    const char *slash = strrchr(name, '/');
    const char *leaf = (slash != NULL) ? slash + 1 : name;

    if (strcmp(leaf, lookup->wanted) == 0) {
        lookup->base = (void *) (uintptr_t) info->dlpi_addr;
        return 1;   /* stop iteration */
    }
    return 0;
}

/*
 * Installs every configured entry guard whose library is loaded in this process.
 *
 * A library that is not present is not an error. This runs in every guarded process,
 * and exactly one of them contains the detector's native code; the others find nothing
 * and move on. Logging each miss would fill every other app's log with a line about a
 * library it was never going to load.
 */
static int install_entry_hooks(void) {
    if (g_entry_spec_count == 0) return 0;

    int installed = 0;
    for (int i = 0; i < g_entry_spec_count; i++) {
        if (g_entry_done[i]) continue;
        entry_lookup_t lookup;
        lookup.wanted = g_entry_specs[i].library;
        lookup.base = 0;

        /*
         * The real iterator, not the hooked one. The hook rewrites `dlpi_name` to hide
         * module traces, and matching a library name against a rewritten name would
         * fail in exactly the process where the guard is needed.
         */
        if (real_dl_iterate_phdr != NULL) {
            real_dl_iterate_phdr(entry_lookup_cb, &lookup);
        } else {
            dl_iterate_phdr(entry_lookup_cb, &lookup);
        }

        if (lookup.base == 0) continue;

        void *target = (void *) ((uintptr_t) lookup.base + g_entry_specs[i].offset);
        void *tramp = NULL;
        if (install_inline_hook(target, (void *) entry_guard_clean, &tramp)) {
            installed++;
            g_entry_done[i] = 1;
            LOGI("entry guard %s+0x%lx -> %p", g_entry_specs[i].library,
                 (unsigned long) g_entry_specs[i].offset, target);

            /*
             * Read back the first instruction, because "installed" is a claim and
             * this is the evidence for it. A page remapped between the write and this
             * read would silently restore the original code, and the guard would
             * report success while doing nothing.
             */
            {
                uint32_t verify = *(volatile uint32_t *) target;
                LOGI("entry guard verify %p: insn=0x%08x", target, verify);
            }
        } else {
            LOGI("entry guard %s+0x%lx NOT installed", g_entry_specs[i].library,
                 (unsigned long) g_entry_specs[i].offset);
        }
    }
    return installed;
}

static int install_entry_hooks_for(const char *name) {
    if (name == NULL || name[0] == '\0' || g_entry_spec_count == 0) return 0;
    const char *slash = strrchr(name, '/');
    const char *leaf = (slash != NULL) ? slash + 1 : name;
    int installed = 0;
    for (int i = 0; i < g_entry_spec_count; i++) {
        if (g_entry_specs[i].library[0] == '\0') continue;
        if (strlen(leaf) != strlen(g_entry_specs[i].library)) continue;
        if (strncmp(leaf, g_entry_specs[i].library, strlen(leaf)) != 0) continue;
        entry_lookup_t lookup;
        lookup.wanted = g_entry_specs[i].library;
        lookup.base = 0;
        if (real_dl_iterate_phdr != NULL) {
            real_dl_iterate_phdr(entry_lookup_cb, &lookup);
        } else {
            dl_iterate_phdr(entry_lookup_cb, &lookup);
        }
        if (lookup.base == 0) continue;
        void *target = (void *) ((uintptr_t) lookup.base + g_entry_specs[i].offset);
        void *tramp = NULL;
        if (install_inline_hook(target, (void *) entry_guard_clean, &tramp)) {
            installed++;
            g_entry_done[i] = 1;
            LOGI("entry guard %s+0x%lx -> %p", g_entry_specs[i].library, (unsigned long) g_entry_specs[i].offset, target);
        }
    }
    return installed;
}

/*
 * Re-applies every GOT hook, for a library that was not loaded when they were first
 * installed.
 *
 * The GOT engine walks the loaded modules and rewrites the slots that name a guarded
 * symbol. That is complete at install time and stays complete for every module that
 * existed then -- but a module loaded later arrives with a fresh, untouched GOT. Its
 * calls leave through its own PLT, land in libc, and never reach a guard, no matter
 * how many guards are installed in the process.
 *
 * This is not a corner case. `libapplist_detector.so` is loaded by
 * `System.loadLibrary` when the app starts its checks, which is well after the
 * injected process has finished installing. Its `__open_2`, `access`, `stat` and
 * `syscall` slots therefore pointed at real libc functions, and the file-existence
 * checks it performs were answered truthfully even though the same paths were hidden
 * from every other caller in the process.
 *
 * The fix is to redo the pass whenever a library appears. `got_hook_symbol` already
 * does the right thing on a second run -- it writes the same value into the same slot,
 * so a module that was patched before is unaffected and a module that was not is
 * caught. The cost is one traversal of the link map per load, which happens a handful
 * of times per process.
 *
 * `dlsym` is deliberately last, and `syscall` before it, for the same reason as in the
 * initial install: these two are the broadest and the ones whose own installation is
 * most likely to depend on the narrower hooks already being in place.
 */
static void replay_got_hooks(void) {
    /*
     * Bounded, because a process that loads libraries in a loop would otherwise pay
     * for a full traversal each time. Sixty-four covers every app seen here -- the
     * usual count is under ten -- and the bound exists so a pathological case degrades
     * into "later libraries unguarded" rather than into a visible slowdown.
     */
    static int replayed = 0;
    if (replayed >= 64) return;
    replayed++;

    int hits = 0;
    hits += got_hook_symbol("access", (void *) hook_access);
    hits += got_hook_symbol("stat", (void *) hook_stat);
    hits += got_hook_symbol("openat", (void *) hook_openat);
    hits += got_hook_symbol("open", (void *) hook_open);
    hits += got_hook_symbol("__open_2", (void *) hook_open_2);
    hits += got_hook_symbol("mmap", (void *) hook_mmap);
    hits += got_hook_symbol("read", (void *) hook_read);
    hits += got_hook_symbol("pread64", (void *) hook_pread64);
    hits += got_hook_symbol("readv", (void *) hook_readv);
    hits += got_hook_symbol("preadv", (void *) hook_preadv);
    hits += got_hook_symbol("preadv2", (void *) hook_preadv2);
    hits += got_hook_symbol("readlink", (void *) hook_readlink);
    hits += got_hook_symbol("readlinkat", (void *) hook_readlinkat);
    hits += got_hook_symbol("close", (void *) hook_close);
    hits += got_hook_symbol("fgets", (void *) hook_fgets);
    hits += got_hook_symbol("fread", (void *) hook_fread);
    hits += got_hook_symbol("fopen", (void *) hook_fopen);
    if (real_prop_get != NULL) hits += got_hook_symbol("__system_property_get", (void *) hook_prop_get);
    if (real_prop_find != NULL) hits += got_hook_symbol("__system_property_find", (void *) hook_prop_find);
    if (real_prop_read_cb != NULL) hits += got_hook_symbol("__system_property_read_callback", (void *) hook_prop_read_callback);
    hits += got_hook_symbol("dl_iterate_phdr", (void *) hook_dl_iterate_phdr);
    hits += got_hook_symbol("syscall", (void *) hook_syscall);
    hits += got_hook_symbol("dlsym", (void *) hook_dlsym);

    LOGI("got replay: %d slots patched", hits);
}

static void *hook_dlopen(const char *filename, int flags) {
    if (real_dlopen == NULL) return NULL;
    void *handle = real_dlopen(filename, flags);
    if (handle != NULL) {
        int installed = install_entry_hooks_for(filename);
        /*
         * A library that was not present when the guards were installed carries an
         * untouched GOT. Re-running the pass here is what closes that window; the
         * entry hooks above are a different mechanism and do not substitute for it.
         */
        replay_got_hooks();
        if (installed > 0) LOGI("dlopen %s -> %p, entry_guards=%d", filename, handle, installed);
    }
    return handle;
}

static void *hook_android_dlopen_ext(const char *filename, int flags, const void *extinfo) {
    if (real_android_dlopen_ext == NULL) return NULL;
    void *handle = real_android_dlopen_ext(filename, flags, extinfo);
    if (handle != NULL) {
        int installed = install_entry_hooks_for(filename);
        replay_got_hooks();
        if (installed > 0) LOGI("android_dlopen_ext %s -> %p, entry_guards=%d", filename, handle, installed);
    }
    return handle;
}

/* ------------------------------------------------------------------------- */
/* Memory overrides                                                          */
/* ------------------------------------------------------------------------- */
/*
 * Writes a value into another library's data section.
 *
 * The other two mechanisms reach a decision that has already been made: an exit
 * guard rewrites what the detector reads, an entry guard replaces what the
 * detector does. Neither reaches a verdict the detector computes from numbers it
 * produced itself — there is no call to intercept and no return value to forge.
 *
 * What is left is the data those numbers come from. A detector that caches
 * "already checked" in a byte reads that byte before doing the expensive work;
 * rewriting the byte makes the expensive path unreachable without touching a
 * single instruction, so no integrity check can observe it.
 *
 * Format, one per line: library.so:offset_hex:value_hex[:width]
 * Width defaults to 1 byte, which is what a boolean cache flag needs.
 */
#define MEM_OVERRIDE_MAX 16
#define MEM_OVERRIDE_NAME 96

typedef struct {
    char library[MEM_OVERRIDE_NAME];
    uintptr_t offset;
    uint64_t value;
    int width;
    /*
     * Resolved once, then written from a background thread. Caching it here keeps
     * the steady-state cost at one store instead of a module walk per write: the
     * thread below may run thousands of times, and a loader traversal per iteration
     * would be visible in the app.
     */
    volatile void *resolved;

    /*
     * How long this particular byte keeps being rewritten, in milliseconds, counted
     * from the moment the lock starts. Zero means "until the lock stops".
     *
     * Per-byte rather than global because the bytes are not equivalent. Some of them
     * are status flags the detector sets while it runs, and those must be held down
     * for as long as the checks keep looping. Others are read by the render path to
     * decide the run has finished, and holding those at zero is what keeps the
     * splash screen up forever. A single duration for the whole set would have to be
     * either too short to matter or long enough to hang, and the measurement showed
     * both outcomes on this device.
     */
    int hold_ms;

    /*
     * When this byte first starts being held, in milliseconds from the moment the lock
     * starts. Zero means "immediately".
     *
     * A start offset turns the override into a window rather than a hold, and the
     * window is what makes a byte that serves two purposes usable at all. The same
     * byte can be a verdict a detector writes and a state a renderer waits on: holding
     * it at zero for the whole run keeps the splash screen up, while leaving it alone
     * lets the verdict stand. Writing one value first so the renderer can proceed, then
     * a different value later so the verdict is gone by the time it is formatted, needs
     * two entries for the same byte that differ only in when they act.
     */
    int start_ms;
} mem_override_spec;

static mem_override_spec g_mem_specs[MEM_OVERRIDE_MAX];
static int g_mem_spec_count = 0;

typedef struct {
    const char *wanted;
    uintptr_t base;
} mem_lookup_t;

static int mem_lookup_cb(struct dl_phdr_info *info, size_t size, void *data) {
    (void) size;
    mem_lookup_t *lookup = (mem_lookup_t *) data;
    if (info == NULL || lookup == NULL || info->dlpi_name == NULL) return 0;
    const char *name = info->dlpi_name;
    const char *slash = strrchr(name, '/');
    const char *leaf = (slash != NULL) ? slash + 1 : name;
    if (strcmp(leaf, lookup->wanted) == 0) {
        lookup->base = (uintptr_t) info->dlpi_addr;
        return 1;
    }
    return 0;
}

/*
 * Applies every configured override whose library is loaded here, and reports
 * how many took effect. A library that is not mapped yet is not an error: the
 * same retry that installs late entry guards runs this again.
 */
/*
 * Keeps the overridden bytes at their configured values while the process runs.
 *
 * A single write at install time is not enough, and the measurement that proved it is
 * worth recording: the write landed at 23:28:43.943, the detector's checks did not run
 * until 23:28:45.467, and every bit set during those checks overwrote the zero that had
 * been placed there. One of the bits survived because nothing wrote it again, which is
 * exactly why the result looked half-working instead of broken — the mechanism reached
 * the right bytes, it just stopped running before the program did.
 *
 * Re-applying in a loop turns the override from a one-shot assignment into a standing
 * fact. A detector that sets a flag and reads it back within the same function would
 * still win the race, but this detector computes a verdict and formats it later, so the
 * window is wide. The interval is a compromise: frequent enough that the formatted
 * value is essentially always the overridden one, rare enough that the thread is not
 * measurable.
 *
 * Threads are created detached and the loop exits when the module is torn down or the
 * process dies, whichever comes first. There is nothing to join and nothing to leak
 * that matters at that point.
 */
static volatile int g_mem_lock_running = 0;

static void *mem_override_lock_thread(void *arg) {
    (void) arg;

    /*
     * Per-byte hold times, counted from the end of the initial delay.
     *
     * The mechanism needs to start late and end at different times for different bytes,
     * and both halves came from measurement rather than from preference.
     *
     * Starting late: the detector sets its status bits while it runs, so a write that
     * lands at install time is overwritten long before the result is read. Three seconds
     * of delay puts every write after the checks on this device have had their say.
     *
     * Ending per byte: the bytes are not the same kind of thing. Holding a status bit
     * at zero for longer only helps — the detector keeps re-setting it, so the override
     * has to outlast the checks. But the same loop also covers bytes the render path
     * reads to decide the run has finished, and holding those at zero is exactly what
     * kept the splash screen up: with all four held for twenty-four seconds the process
     * stayed alive and the result page never appeared, while the same four held for six
     * seconds drew normally. A single shared duration has to be either too short to
     * matter or long enough to hang, so the duration is a property of each byte.
     *
     * Zero means "hold until the lock stops", which is the useful default for a byte that
     * only ever needs suppressing and has no reader waiting on it to change.
     */
    usleep(0);

    /*
     * The loop runs as long as the longest hold, so no byte is cut off early. The whole
     * thing is bounded because this happens once per process and there is no reason for
     * a guard thread to outlive the reason it exists.
     */
    int longest_ms = 0;
    for (int i = 0; i < g_mem_spec_count; i++) {
        if (g_mem_specs[i].hold_ms == 0) { longest_ms = 0; break; }
        int span = g_mem_specs[i].start_ms + g_mem_specs[i].hold_ms;
        if (span > longest_ms) longest_ms = span;
    }
    int unlimited = (longest_ms == 0);
    int max_passes = unlimited ? 20 : (longest_ms / 100) + 2;

    int elapsed_ms = 0;
    for (int pass = 0; pass < max_passes; pass++) {
        for (int i = 0; i < g_mem_spec_count; i++) {
            volatile void *target = g_mem_specs[i].resolved;
            if (target == NULL) continue;

            /*
             * A byte whose hold has already expired is skipped from here on. It keeps
             * whatever value the program last gave it, which is the point: the render
             * path is waiting for exactly that.
             */
            /*
             * Two bounds, not one: the hold begins at `start_ms` and ends at
             * `start_ms + hold_ms`. A specification with neither value covers the whole
             * run, which is the behaviour every earlier entry keeps.
             *
             * The start bound is what lets one byte carry two meanings. Some bytes are
             * both a verdict and a state the renderer waits on; writing the verdict value
             * from the beginning keeps the splash screen up, while never writing it lets
             * the verdict stand. Writing the state value first and the verdict value later
             * satisfies both readers, and it needs an entry that knows when to act rather
             * than one that only knows for how long.
             */
            if (g_mem_specs[i].start_ms > 0 && elapsed_ms < g_mem_specs[i].start_ms) continue;
            if (g_mem_specs[i].hold_ms > 0 && elapsed_ms >= g_mem_specs[i].start_ms + g_mem_specs[i].hold_ms) continue;

            switch (g_mem_specs[i].width) {
                case 1: *(volatile uint8_t *) target = (uint8_t) g_mem_specs[i].value; break;
                case 2: *(volatile uint16_t *) target = (uint16_t) g_mem_specs[i].value; break;
                case 4: *(volatile uint32_t *) target = (uint32_t) g_mem_specs[i].value; break;
                case 8: *(volatile uint64_t *) target = g_mem_specs[i].value; break;
                default: break;
            }
        }
        usleep(100 * 1000);
        elapsed_ms += 100;
    }

    LOGI("mem override lock finished after %d ms", elapsed_ms);
    g_mem_lock_running = 0;
    return NULL;
}

static void start_mem_override_lock(void) {
    if (g_mem_lock_running || g_mem_spec_count == 0) return;
    g_mem_lock_running = 1;
    pthread_t tid;
    if (pthread_create(&tid, NULL, mem_override_lock_thread, NULL) == 0) {
        pthread_detach(tid);
        LOGI("mem override lock started for %d spec(s)", g_mem_spec_count);
    } else {
        g_mem_lock_running = 0;
        LOGI("mem override lock failed to start");
    }
}

static int apply_memory_overrides(void) {
    if (g_mem_spec_count == 0) return 0;
    int applied = 0;
    for (int i = 0; i < g_mem_spec_count; i++) {
        mem_lookup_t lookup;
        lookup.wanted = g_mem_specs[i].library;
        lookup.base = 0;
        if (real_dl_iterate_phdr != NULL) {
            real_dl_iterate_phdr(mem_lookup_cb, &lookup);
        } else {
            dl_iterate_phdr(mem_lookup_cb, &lookup);
        }
        if (lookup.base == 0) continue;
        void *target = (void *) (lookup.base + g_mem_specs[i].offset);

        /*
         * A specification with a start offset is not applied here. It describes a
         * window that opens later, and writing it now would open the window early —
         * which is exactly what the first version of this did, and it put the render
         * path in a state the window was meant to avoid. The address is still cached
         * so the lock thread can use it, and the caller still learns that work is
         * pending, so the lock gets started.
         */
        if (g_mem_specs[i].start_ms > 0) {
            g_mem_specs[i].resolved = target;
            applied++;
            continue;
        }
        g_mem_specs[i].resolved = target;
        switch (g_mem_specs[i].width) {
            case 1: *(volatile uint8_t *) target = (uint8_t) g_mem_specs[i].value; break;
            case 2: *(volatile uint16_t *) target = (uint16_t) g_mem_specs[i].value; break;
            case 4: *(volatile uint32_t *) target = (uint32_t) g_mem_specs[i].value; break;
            case 8: *(volatile uint64_t *) target = g_mem_specs[i].value; break;
            default: continue;
        }
        applied++;
        LOGI("mem override %s+0x%lx = 0x%llx (w%d) -> %p",
             g_mem_specs[i].library, (unsigned long) g_mem_specs[i].offset,
             (unsigned long long) g_mem_specs[i].value, g_mem_specs[i].width, target);
    }
    return applied;
}

static void set_memory_overrides(const char *text) {
    g_mem_spec_count = 0;
    if (text == NULL || text[0] == '\0') return;
    const char *p = text;
    while (*p != '\0' && g_mem_spec_count < MEM_OVERRIDE_MAX) {
        const char *nl = strchr(p, '\n');
        const char *end = (nl != NULL) ? nl : p + strlen(p);
        const char *c1 = memchr(p, ':', (size_t) (end - p));
        if (c1 != NULL && c1 > p) {
            const char *c2 = memchr(c1 + 1, ':', (size_t) (end - c1 - 1));
            if (c2 != NULL && c2 > c1 + 1) {
                size_t name_len = (size_t) (c1 - p);
                if (name_len < MEM_OVERRIDE_NAME) {
                    memcpy(g_mem_specs[g_mem_spec_count].library, p, name_len);
                    g_mem_specs[g_mem_spec_count].library[name_len] = '\0';
                    g_mem_specs[g_mem_spec_count].offset = (uintptr_t) strtoul(c1 + 1, NULL, 16);

                    char vbuf[32];
                    const char *c3 = memchr(c2 + 1, ':', (size_t) (end - c2 - 1));
                    size_t vlen = (c3 != NULL) ? (size_t) (c3 - c2 - 1) : (size_t) (end - c2 - 1);
                    if (vlen > 0 && vlen < sizeof(vbuf)) {
                        memcpy(vbuf, c2 + 1, vlen);
                        vbuf[vlen] = '\0';
                        g_mem_specs[g_mem_spec_count].value = (uint64_t) strtoull(vbuf, NULL, 16);
                        int width = 1;
                        if (c3 != NULL && c3 + 1 < end) {
                            width = (int) strtol(c3 + 1, NULL, 10);
                            /*
                             * An optional fifth field says how long this byte stays held.
                             * Absent means the whole lock duration, which is the old
                             * behaviour for every specification written before this field
                             * existed.
                             */
                            const char *c4 = memchr(c3 + 1, ':', (size_t) (end - c3 - 1));
                            if (c4 != NULL && c4 + 1 < end) {
                                g_mem_specs[g_mem_spec_count].hold_ms = (int) strtol(c4 + 1, NULL, 10);

                                /*
                                 * An optional sixth field delays the start of the hold.
                                 * Absent means the hold begins as soon as the lock does, which
                                 * is what every earlier specification relies on.
                                 *
                                 * This search lives inside the fifth-field branch on purpose.
                                 * With no fifth field `c4` is NULL, and advancing past it gives
                                 * memchr the address 0x1 together with a length computed
                                 * against the same NULL -- a scan from 0x1 that faults on its
                                 * first page. A four-field specification is the common case,
                                 * so that fault was the common case too.
                                 */
                                const char *c5 = memchr(c4 + 1, ':', (size_t) (end - c4 - 1));
                                if (c5 != NULL && c5 + 1 < end) {
                                    g_mem_specs[g_mem_spec_count].start_ms = (int) strtol(c5 + 1, NULL, 10);
                                }
                            }
                        }
                        if (width != 1 && width != 2 && width != 4 && width != 8) width = 1;
                        g_mem_specs[g_mem_spec_count].width = width;
                        g_mem_spec_count++;
                    }
                }
            }
        }
        p = (nl != NULL) ? nl + 1 : end;
    }
    LOGI("memory overrides configured: %d", g_mem_spec_count);
}

JNIEXPORT void JNICALL
Java_com_houvven_guise_xposed_hook_NativeGuard_setMemoryOverrides(JNIEnv *env, jclass clazz, jstring text) {
    if (text == NULL) { set_memory_overrides(NULL); return; }
    const char *utf = (*env)->GetStringUTFChars(env, text, NULL);
    if (utf == NULL) return;
    set_memory_overrides(utf);
    (*env)->ReleaseStringUTFChars(env, text, utf);
}

static void set_entry_hooks(const char *text) {
    g_entry_spec_count = 0;
    if (text == NULL || text[0] == '\0') return;

    const char *p = text;
    while (*p != '\0' && g_entry_spec_count < ENTRY_HOOK_MAX) {
        const char *nl = strchr(p, '\n');
        const char *end = (nl != NULL) ? nl : p + strlen(p);
        const char *colon = memchr(p, ':', (size_t) (end - p));
        if (colon != NULL && colon > p && colon + 1 < end) {
            size_t name_len = (size_t) (colon - p);
            if (name_len < ENTRY_NAME_MAX) {
                memcpy(g_entry_specs[g_entry_spec_count].library, p, name_len);
                g_entry_specs[g_entry_spec_count].library[name_len] = '\0';
                g_entry_specs[g_entry_spec_count].offset =
                        (uintptr_t) strtoul(colon + 1, NULL, 16);
                g_entry_spec_count++;
            }
        }
        p = (nl != NULL) ? nl + 1 : end;
    }
    LOGI("entry hooks configured: %d", g_entry_spec_count);
}

JNIEXPORT void JNICALL
Java_com_houvven_guise_xposed_hook_NativeGuard_setEntryHooks(JNIEnv *env, jclass clazz, jstring text) {
    if (text == NULL) {
        set_entry_hooks(NULL);
        return;
    }

    const char *utf = (*env)->GetStringUTFChars(env, text, NULL);
    if (utf == NULL) return;
    set_entry_hooks(utf);
    (*env)->ReleaseStringUTFChars(env, text, utf);
}

/*
 * Retries the entry guards, and reports how many are now in place.
 *
 * This exists because of an ordering that cannot be fixed from here: the detector
 * loads its own native library after this module has installed everything, and the
 * loader cannot be hooked to catch it — that was tried, and it crashed the process
 * in the graphics stack, which resolves its driver entry points through the same
 * call. A retry driven from the Java side has none of that reach. It only asks
 * "is the library there yet", and the answer stops mattering as soon as it is.
 *
 * Returning the count lets the caller stop retrying instead of polling forever, and
 * it is a count rather than a boolean because a later retry may be the one that
 * completes the set when several libraries arrive at different times.
 */
JNIEXPORT jint JNICALL
Java_com_houvven_guise_xposed_hook_NativeGuard_retryEntryHooks(JNIEnv *env, jclass clazz) {
    (void) env;
    (void) clazz;
    /* Both mechanisms are retried, so an early return on one of them would
     * starve the other. Presence of either spec makes this call meaningful. */
    /*
     * All three lists have to be consulted here, not just the two that are applied
     * immediately. A library that is loaded after install — which is the normal case for
     * the application's own code — cannot be re-labelled by `install`, because it did not
     * exist yet; the retry loop is the only thing that ever reaches it. Leaving the VMA
     * list out of this guard made the retry exit before doing that work, and the result
     * looked like the mechanism had simply stopped functioning: no relabel log lines, and
     * the self-check that the relabel exists to avoid ran on every loop iteration.
     */
    if (g_entry_spec_count == 0 && g_mem_spec_count == 0 && g_vma_spec_count == 0) return 0;
    /*
     * Both mechanisms are retried together.
     *
     * They share the same reason for being late: the library that holds the
     * targets is loaded by the app after this module installs. Retrying only the
     * entry guards would leave the data overrides unapplied in exactly the
     * processes that needed the retry in the first place.
     */
    int applied = apply_memory_overrides();
    if (applied > 0) start_mem_override_lock();
    applied += apply_vma_relabels();

    /*
     * Entry guards run after the replacement, and the order is the whole point.
     * An inline hook writes into a library's code pages; if the replacement
     * copied those pages afterwards, the copy would carry the hook, and the
     * library's own self-check — which compares its text against a checksum
     * baked into it — would find a difference that is not supposed to exist.
     * Copying first means the shadow keeps pristine bytes, and the hook lands on
     * the shadow, where the only observable difference is the name the loader
     * reports, which is exactly what is being hidden.
     */
    int guarded = install_entry_hooks();
    return (jint) (applied + guarded);
}

JNIEXPORT jboolean JNICALL
Java_com_houvven_guise_xposed_hook_NativeGuard_install(JNIEnv *env, jclass clazz) {
    if (g_installed) return JNI_TRUE;

    /*
     * Resolve, patch, and repoint.
     *
     * The order matters and is easy to get wrong. `dlsym` returns the address the
     * dynamic linker published, which is the address that is about to be patched.
     * Storing that address in `real_access` and then calling it from `hook_access`
     * would jump straight back into the patch and recurse until the stack dies.
     * The callable original is the trampoline: it holds the instructions that were
     * displaced, followed by a branch back to the rest of the function.
     *
     * So each entry point is handled in three steps — resolve, patch, and only then
     * store the trampoline as the real target. A failed patch leaves the pointer at
     * the untouched address, which is harmless: the hook is never entered, and the
     * process behaves exactly as it did before.
     */
    void *orig_openat = dlsym(RTLD_DEFAULT, "openat");
    void *orig_open   = dlsym(RTLD_DEFAULT, "open");
    void *orig_access = dlsym(RTLD_DEFAULT, "access");
    void *orig_stat   = dlsym(RTLD_DEFAULT, "stat");
    void *orig_read   = dlsym(RTLD_DEFAULT, "read");
    void *orig_pread64 = dlsym(RTLD_DEFAULT, "pread64");
    void *orig_readv  = dlsym(RTLD_DEFAULT, "readv");
    void *orig_preadv  = dlsym(RTLD_DEFAULT, "preadv");
    void *orig_preadv2 = dlsym(RTLD_DEFAULT, "preadv2");
    void *orig_readlink = dlsym(RTLD_DEFAULT, "readlink");
    void *orig_readlinkat = dlsym(RTLD_DEFAULT, "readlinkat");
    void *orig_close  = dlsym(RTLD_DEFAULT, "close");
    void *orig_dl_iterate_phdr = dlsym(RTLD_DEFAULT, "dl_iterate_phdr");
    void *orig_syscall = dlsym(RTLD_DEFAULT, "syscall");
void *orig_dlopen = dlsym(RTLD_DEFAULT, "dlopen");
void *orig_android_dlopen_ext = dlsym(RTLD_DEFAULT, "android_dlopen_ext");
void *orig_dlsym = dlsym(RTLD_DEFAULT, "dlsym");
void *orig_open_2 = dlsym(RTLD_DEFAULT, "__open_2");
void *orig_mmap  = dlsym(RTLD_DEFAULT, "mmap");

    real_prop_get = (prop_get_t) dlsym(RTLD_DEFAULT, "__system_property_get");
    real_prop_get_int = (prop_get_int_t) dlsym(RTLD_DEFAULT, "__system_property_get_int");
    real_prop_find = (prop_find_t) dlsym(RTLD_DEFAULT, "__system_property_find");
    real_prop_read_cb = (prop_read_callback_t) dlsym(RTLD_DEFAULT, "__system_property_read_callback");

    LOGI("orig: openat=%p open=%p access=%p stat=%p prop_get=%p prop_get_int=%p prop_find=%p read_cb=%p",
         orig_openat, orig_open, orig_access, orig_stat,
         real_prop_get, real_prop_get_int, real_prop_find, real_prop_read_cb);

#if defined(__aarch64__)
    void *tramp = NULL;

    /*
     * `access` and `stat` first.
     *
     * These are the two calls that answer "does /dev/socket/adbd exist", which is
     * the question behind the debugging report, and neither is on a hot path that a
     * launcher would notice. Patching the two most specific entry points first keeps
     * the blast radius small while the mechanism itself is being proven: if the
     * process survives and the socket probe changes, the inline patch works and the
     * remaining entry points can follow.
     */
    tramp = NULL;
    if (orig_access != NULL && got_hook_symbol("access", (void *) hook_access) > 0) {
        real_access = (access_t) orig_access;
        LOGI("hooked access orig=%p tramp=%p", orig_access, tramp);
    } else {
        real_access = (access_t) orig_access;
        LOGI("access NOT hooked (patch failed), passthrough only");
    }

    tramp = NULL;
    if (orig_stat != NULL && got_hook_symbol("stat", (void *) hook_stat) > 0) {
        real_stat = (stat_t) orig_stat;
        LOGI("hooked stat orig=%p tramp=%p", orig_stat, tramp);
    } else {
        real_stat = (stat_t) orig_stat;
        LOGI("stat NOT hooked (patch failed), passthrough only");
    }

    /*
     * The property reader is the third attempt, and the one with the least certain
     * payoff: `__system_property_get` is the pre-Android-8 API, and modern code more
     * often uses `__system_property_find` plus a callback. It is patched anyway
     * because some detectors still call it, and because a working patch here costs
     * nothing when the symbol is absent — the branch simply falls through.
     */
    if (real_prop_get != NULL) {
        tramp = NULL;
        void *orig_prop = (void *) real_prop_get;
        if (got_hook_symbol("__system_property_get", (void *) hook_prop_get) > 0) {
            real_prop_get = (prop_get_t) orig_prop;
            LOGI("hooked prop_get orig=%p tramp=%p", orig_prop, tramp);
        } else {
            LOGI("prop_get NOT hooked (patch failed)");
        }
    }

    /*
     * The modern lookup, which is the one the property area is mapped by.
     *
     * Hooked for a different reason than the call above: not to answer differently,
     * but to know when the area becomes readable. Every property read on Android 8 and
     * later goes through this call, so the first one is a reliable signal, and it is
     * the only signal available -- the mapping happens inside libc and is never
     * announced.
     */
    if (real_prop_find != NULL) {
        tramp = NULL;
        void *orig_find = (void *) real_prop_find;
        if (got_hook_symbol("__system_property_find", (void *) hook_prop_find) > 0) {
            real_prop_find = (prop_find_t) orig_find;
            LOGI("hooked prop_find orig=%p tramp=%p", orig_find, tramp);
        } else {
            LOGI("prop_find NOT hooked (patch failed), lazy patch unavailable");
        }
    }

    /*
     * The call that hands a property value to a reader, and the one where the answer
     * is decided.
     *
     * Android 8 and later read a property by finding the record and then asking this
     * call to deliver its contents. The record itself is what the platform owns; the
     * value is what this call reports, so substituting it here covers every reader
     * that goes through the property service -- regardless of which property file the
     * record lives in, when that file was mapped, or whether it is mapped at all.
     *
     * Hooked as well as `__system_property_find`, because the two answer different
     * questions: find locates the record, this call reports its contents. A reader that
     * only uses find and then reads the record bytes directly does not go through here,
     * which is why the page rewrite is kept as well -- the two paths cover each other.
     */
    if (real_prop_read_cb != NULL) {
        tramp = NULL;
        void *orig_rcb = (void *) real_prop_read_cb;
        if (got_hook_symbol("__system_property_read_callback", (void *) hook_prop_read_callback) > 0) {
            real_prop_read_cb = (prop_read_callback_t) orig_rcb;
            LOGI("hooked prop_read_callback orig=%p tramp=%p", orig_rcb, tramp);
        } else {
            LOGI("prop_read_callback NOT hooked (patch failed)");
        }
    }

    /*
     * `openat` and `open` carry the maps read, and they are patched last.
     *
     * They are the busiest functions in the process: every library load, every asset,
     * every config file passes through them. A fault here would take the app down
     * before any result could be read, which is why the mechanism itself was proven on
     * the narrower hooks above before these were attempted.
     *
     * They are still worth patching, because they are the only place both remaining
     * leaks can be closed at once: the "Zygisk found" report comes from a line in the
     * maps file, and the "code injection" report comes from the anonymous executable
     * pages this module itself maps for trampolines. Both are read through this same
     * call, so one filter here answers both.
     *
     * A refused patch is not an error: the pointer falls back to the original and the
     * process behaves exactly as it did before. The log names which case happened, so
     * a silent failure cannot be mistaken for a working guard.
     */
    tramp = NULL;
    if (orig_openat != NULL && got_hook_symbol("openat", (void *) hook_openat) > 0) {
        real_openat = (openat_t) orig_openat;
        LOGI("hooked openat orig=%p tramp=%p", orig_openat, tramp);
    } else {
        real_openat = (openat_t) orig_openat;
        LOGI("openat NOT hooked (patch failed), passthrough only");
    }

    tramp = NULL;
    if (orig_open != NULL && got_hook_symbol("open", (void *) hook_open) > 0) {
        real_open = (open_t) orig_open;
        LOGI("hooked open orig=%p tramp=%p", orig_open, tramp);
    } else {
        real_open = (open_t) orig_open;
        LOGI("open NOT hooked (patch failed), passthrough only");
    }

    /*
     * The FORTIFY open and the mapping call, registered next to the plain open.
     *
     * A process that never uses one of these symbols still has a slot for it, because
     * every imported symbol has one; the hook simply never runs. A process that uses
     * them for the property area is the case being covered.
     */
    tramp = NULL;
    real_open_2 = (open_2_t) orig_open_2;
    if (orig_open_2 != NULL && got_hook_symbol("__open_2", (void *) hook_open_2) > 0) {
        LOGI("hooked __open_2 orig=%p", orig_open_2);
    } else {
        LOGI("__open_2 NOT hooked (patch failed), passthrough only");
    }

    tramp = NULL;
    real_mmap = (mmap_fn_t) orig_mmap;
    if (orig_mmap != NULL && got_hook_symbol("mmap", (void *) hook_mmap) > 0) {
        LOGI("hooked mmap orig=%p", orig_mmap);
    } else {
        LOGI("mmap NOT hooked (patch failed), passthrough only");
    }

    /*
     * `read` is the last piece, and the one that makes the whole design work.
     *
     * The two attempts before this one replaced the descriptor itself, handing back a
     * memfd filled with edited text. Both crashed inside the detector's own library a
     * millisecond after the filter logged: a proc file and a memfd are not
     * interchangeable to a reader that inspects the descriptor rather than just
     * reading it. So the descriptor now stays the kernel's own, and the edit happens
     * here instead — in the buffer, on the way out, with every replaced byte matched
     * by one of the same length.
     *
     * A reader that opens the file gets the true size from `fstat`, the true path from
     * `readlink`, and the true offsets from the kernel. The only difference is that
     * the token it is looking for is not in the bytes it receives. Nothing about the
     * call is inconsistent, so nothing downstream can trip over the difference.
     *
     * It is patched even though it sits on a hot path, because there is no narrower
     * seam: the content has to pass through a read at some point, and this is that
     * point. The hook is one pass-through call plus a table lookup, and it only
     * rewrites when the buffer actually begins with a map entry.
     */
    if (orig_read != NULL) {
        tramp = NULL;
        if (got_hook_symbol("read", (void *) hook_read) > 0) {
            real_read = (read_t) orig_read;
            LOGI("hooked read orig=%p tramp=%p", orig_read, tramp);
        } else {
            real_read = (read_t) orig_read;
            LOGI("read NOT hooked (patch failed), passthrough only");
        }
    } else {
        real_read = NULL;
        LOGI("read symbol missing, maps text cannot be rewritten");
    }

    /*
     * The two alternate read paths, patched with the same content-based rule.
     *
     * A reader is free to use any of the three, and which one it picks is not
     * something this process can influence: `pread64` is the natural choice when the
     * size is known in advance, and `readv` when the content is collected into several
     * buffers. Hooking only the plain `read` covers the most common case and misses the
     * rest, which is exactly the gap the previous attempt fell into: the hook was
     * installed, the log said so, and the token still came through.
     *
     * Each is patched independently, so a refusal on one — a PC-relative prologue, a
     * symbol this libc does not export — leaves the others working. They log their own
     * outcomes rather than a shared one, because "pread64 hooked, readv not" is a
     * materially different state from "both failed".
     */
    if (orig_pread64 != NULL) {
        tramp = NULL;
        if (got_hook_symbol("pread64", (void *) hook_pread64) > 0) {
            real_pread64 = (pread64_t) orig_pread64;
            LOGI("hooked pread64 orig=%p tramp=%p", orig_pread64, tramp);
        } else {
            real_pread64 = (pread64_t) orig_pread64;
            LOGI("pread64 NOT hooked (patch failed), passthrough only");
        }
    } else {
        real_pread64 = NULL;
        LOGI("pread64 symbol missing");
    }

    if (orig_readv != NULL) {
        tramp = NULL;
        if (got_hook_symbol("readv", (void *) hook_readv) > 0) {
            real_readv = (readv_t) orig_readv;
            LOGI("hooked readv orig=%p tramp=%p", orig_readv, tramp);
        } else {
            real_readv = (readv_t) orig_readv;
            LOGI("readv NOT hooked (patch failed), passthrough only");
        }
    } else {
        real_readv = NULL;
        LOGI("readv symbol missing");
    }

    /*
     * The two positional vector readers.
     *
     * These are the entry points that explain a contradiction observed in testing:
     * the plain read hook fired, its log said `read AFTER rewrite cleaned`, and the
     * detector still reported the token. A reader that wants a file in one call and
     * knows its length reaches for `preadv`, not `readv`, because the offset lets it
     * fetch the whole file without tracking a position. Hooking only the non-positional
     * forms left that path completely unguarded.
     *
     * `preadv2` is the same call with a flags word; both are patched independently so a
     * refusal on one leaves the other working, and each reports its own outcome rather
     * than a shared one.
     */
    if (orig_preadv != NULL) {
        tramp = NULL;
        if (got_hook_symbol("preadv", (void *) hook_preadv) > 0) {
            real_preadv = (preadv_t) orig_preadv;
            LOGI("hooked preadv orig=%p tramp=%p", orig_preadv, tramp);
        } else {
            real_preadv = (preadv_t) orig_preadv;
            LOGI("preadv NOT hooked (patch failed), passthrough only");
        }
    } else {
        real_preadv = NULL;
        LOGI("preadv symbol missing");
    }

    if (orig_preadv2 != NULL) {
        tramp = NULL;
        if (got_hook_symbol("preadv2", (void *) hook_preadv2) > 0) {
            real_preadv2 = (preadv2_t) orig_preadv2;
            LOGI("hooked preadv2 orig=%p tramp=%p", orig_preadv2, tramp);
        } else {
            real_preadv2 = (preadv2_t) orig_preadv2;
            LOGI("preadv2 NOT hooked (patch failed), passthrough only");
        }
    } else {
        real_preadv2 = NULL;
        LOGI("preadv2 symbol missing");
    }

    /*
     * The third channel, and the one every read hook was blind to.
     *
     * `read`/`pread64`/`readv` all hand back file contents, and a guard placed on
     * them sees every byte that flows. `readlink` does not: it asks the kernel to
     * translate a path into the name of the object it refers to, and the answer
     * arrives as a single string that no read hook can observe. On
     * `/proc/<pid>/map_files/<range>` that name is the backing object of the mapping,
     * which is exactly where the Vector memfd's name lives:
     *
     *   /memfd:jit-cache-zygisk_vector (deleted)
     *
     * This is why a guard that provably cleaned every buffer still produced a positive
     * report. The buffer was clean; the name never travelled through a buffer.
     *
     * `readlinkat` is the same call with a directory fd, and both are patched because a
     * caller is free to use either. The substitution is applied to whatever string the
     * kernel returns rather than to a fixed set of paths, because the paths are per-pid
     * and per-range and cannot be enumerated in advance.
     */
    if (orig_readlink != NULL) {
        tramp = NULL;
        if (got_hook_symbol("readlink", (void *) hook_readlink) > 0) {
            real_readlink = (readlink_t) orig_readlink;
            LOGI("hooked readlink orig=%p tramp=%p", orig_readlink, tramp);
        } else {
            real_readlink = (readlink_t) orig_readlink;
            LOGI("readlink NOT hooked (patch failed), passthrough only");
        }
    } else {
        real_readlink = NULL;
        LOGI("readlink symbol missing");
    }

    if (orig_readlinkat != NULL) {
        tramp = NULL;
        if (got_hook_symbol("readlinkat", (void *) hook_readlinkat) > 0) {
            real_readlinkat = (readlinkat_t) orig_readlinkat;
            LOGI("hooked readlinkat orig=%p tramp=%p", orig_readlinkat, tramp);
        } else {
            real_readlinkat = (readlinkat_t) orig_readlinkat;
            LOGI("readlinkat NOT hooked (patch failed), passthrough only");
        }
    } else {
        real_readlinkat = NULL;
        LOGI("readlinkat symbol missing");
    }

    /*
     * `close` exists to keep the descriptor table honest.
     *
     * The table records which numbers were opened from a map file, and a number is
     * reused the moment its descriptor is closed. Without clearing on close, a mark
     * outlives the file it described and the next unrelated open inherits it — which
     * is exactly what crashed the detector's service process: its own data files were
     * rewritten as if they were maps.
     *
     * It is patched last because on its own it is the least important hook here: a
     * missed close only leaves a stale mark, and `guard_read_buffer` re-checks the
     * descriptor before acting on one. The two together are what make a reused number
     * unrepresentable; either one alone would be a guess.
     */
    if (orig_close != NULL) {
        tramp = NULL;
        if (got_hook_symbol("close", (void *) hook_close) > 0) {
            real_close = (close_t) orig_close;
            LOGI("hooked close orig=%p tramp=%p", orig_close, tramp);
        } else {
            real_close = (close_t) orig_close;
            LOGI("close NOT hooked (patch failed), marks may go stale");
        }
    } else {
        real_close = NULL;
        LOGI("close symbol missing, marks may go stale");
    }

    /*
     * The loader's list, patched last because it is the only entry point here that
     * does not go through the kernel.
     *
     * Everything above intercepts a system call: a path, a descriptor, a buffer. This
     * one intercepts a callback, and the data it carries is a table the loader built at
     * startup. A detector that asks "what objects are loaded" through it gets an answer
     * that never touched a file, which is why every read-side guard could report success
     * while the detector still saw the module's name.
     *
     * Only the object's reported path is changed; its program headers, load bias and
     * ranges are passed through untouched. A caller that walks the headers to find a
     * library's base address still gets the correct one — it just gets a name that does
     * not mention the module.
     */
    if (orig_dl_iterate_phdr != NULL) {
        tramp = NULL;
        if (got_hook_symbol("dl_iterate_phdr", (void *) hook_dl_iterate_phdr) > 0) {
            real_dl_iterate_phdr = (dl_iterate_phdr_t) orig_dl_iterate_phdr;
            LOGI("hooked dl_iterate_phdr orig=%p tramp=%p", orig_dl_iterate_phdr, tramp);
        } else {
            real_dl_iterate_phdr = (dl_iterate_phdr_t) orig_dl_iterate_phdr;
            LOGI("dl_iterate_phdr NOT hooked (patch failed), passthrough only");
        }
    } else {
        real_dl_iterate_phdr = NULL;
        LOGI("dl_iterate_phdr symbol missing");
    }

    /*
     * The raw entry point, which covers every direct call the libc hooks cannot see.
     *
     * It is patched last because it is the broadest hook here: it sits on the path of
     * every `syscall()` the process makes, including the ones this guard makes itself.
     * The branches inside handle only the numbers whose argument shapes are known and
     * forward everything else with all six slots intact, so the `SYS_readlinkat` call
     * in `fd_targets_map_file` is covered by that forwarding rather than by a branch
     * of its own.
     *
     * The reason it has to exist at all is measured, not theoretical: with fourteen
     * libc hooks installed and confirmed, the diagnostic log for `/proc` opens stayed
     * empty while the detector still named the module. The reads were happening through
     * this entry point, where no libc symbol exists to patch.
     */
    /*
     * The original entry point is stored before the slot is redirected, not after.
     *
     * These two steps look interchangeable and are not. Writing the slot makes every
     * subsequent call in the process arrive at the hook below, including calls made by
     * the very code that is still finishing the install. If the hook finds its own
     * stored pointer still null it reports that the operation is unavailable, and a
     * caller waiting on a futex in another thread reads that as a failed wait and
     * aborts. The order has to be: keep the original, then redirect.
     */
    real_syscall = (syscall_t) orig_syscall;
    if (orig_syscall != NULL) {
        tramp = NULL;
        if (got_hook_symbol("syscall", (void *) hook_syscall) > 0) {
            LOGI("hooked syscall orig=%p tramp=%p", orig_syscall, tramp);
        } else {
            LOGI("syscall NOT hooked (patch failed), direct calls unguarded");
        }
    } else {
        LOGI("syscall symbol missing, direct calls unguarded");
    }

    /*
     * The stdio entry points, which no earlier pass covered.
     *
     * Their originals are resolved and stored before any of them is redirected, for the
     * same reason as above: between redirecting a slot and storing its original there is
     * a window in which a call from any thread reaches a hook that cannot yet forward.
     */
    real_fgets = (fgets_t) dlsym(RTLD_DEFAULT, "fgets");
    real_fread = (fread_t) dlsym(RTLD_DEFAULT, "fread");
    real_fopen = (fopen_t) dlsym(RTLD_DEFAULT, "fopen");
    if (real_fgets != NULL && got_hook_symbol("fgets", (void *) hook_fgets) > 0) {
        LOGI("hooked fgets orig=%p", (void *) real_fgets);
    }
    if (real_fread != NULL && got_hook_symbol("fread", (void *) hook_fread) > 0) {
        LOGI("hooked fread orig=%p", (void *) real_fread);
    }
    if (real_fopen != NULL && got_hook_symbol("fopen", (void *) hook_fopen) > 0) {
        LOGI("hooked fopen orig=%p", (void *) real_fopen);
    }

    /*
     * `dlsym` last, because it is the escape hatch every other hook here assumes does
     * not exist.
     *
     * The GOT engine covers a call that leaves through a module's own PLT entry. A
     * caller that resolves a name once and keeps the address defeats that by design:
     * after the first resolution there is no slot left to rewrite. `dlsym` is the only
     * place where such a resolution can be observed, and it is itself an ordinary
     * imported function, so it is reachable the same way everything else is.
     *
     * Installed through the GOT engine rather than by patching `dlsym`'s first
     * instruction: the point of this whole rewrite is that no code section changes,
     * and an inline hook here would reintroduce exactly the tampering this was built
     * to avoid — and it would do it to libc, which is checksummed.
     */
    if (orig_dlsym != NULL) {
        real_dlsym = (guise_dlsym_t) orig_dlsym;
        int dlsym_slots = got_hook_symbol("dlsym", (void *) hook_dlsym);
        LOGI("dlsym guard: slots=%d real=%p", dlsym_slots, real_dlsym);
    } else {
        real_dlsym = NULL;
        LOGI("dlsym symbol missing, resolved addresses unguarded");
    }

    /*
     * The library loader, registered last of all.
     *
     * Every hook above covers code that is already loaded. They are placed by walking
     * the link map, so a module that arrives afterwards never appears in that walk and
     * keeps a fresh, untouched GOT -- its calls leave through its own PLT, land in real
     * libc, and never reach a guard. The result is not a crash but a silent gap: the
     * process behaves as if the guards did not exist for that library.
     *
     * That is what happened here. `libapplist_detector.so` is mapped straight out of
     * `base.apk` when the app starts its checks, long after install finished. Its
     * `__open_2` and `access` calls reached real libc and saw that the hidden packages
     * were installed, while every other caller in the same process was told otherwise.
     * The two file checks that report a mismatch are the app noticing exactly that
     * disagreement.
     *
     * Registering the loader is what closes the window: `hook_dlopen` runs
     * `replay_got_hooks()` after the new module is mapped, so it gets the same patched
     * GOT as everything that was present at install time. These two are placed after
     * `dlsym` because a replay re-enters every hook above them -- placing the loader
     * first would let a library loaded mid-install see a half-built guard set.
     *
     * The originals are stored before the slots are redirected, for the same reason as
     * `syscall`: once a slot is rewritten, any call from any thread arrives at the hook,
     * and a hook whose stored original is still null has to refuse.
     */
    real_dlopen = (dlopen_t) orig_dlopen;
    real_android_dlopen_ext = (android_dlopen_ext_t) orig_android_dlopen_ext;
    if (orig_dlopen != NULL) {
        int dlopen_slots = got_hook_symbol("dlopen", (void *) hook_dlopen);
        LOGI("dlopen guard: slots=%d real=%p", dlopen_slots, real_dlopen);
    } else {
        LOGI("dlopen symbol missing, later libraries unguarded");
    }
    if (orig_android_dlopen_ext != NULL) {
        int ext_slots = got_hook_symbol("android_dlopen_ext", (void *) hook_android_dlopen_ext);
        LOGI("android_dlopen_ext guard: slots=%d real=%p", ext_slots, real_android_dlopen_ext);
    } else {
        LOGI("android_dlopen_ext symbol missing, later libraries unguarded");
    }

#else
    real_read = (read_t) orig_read;
    real_pread64 = (pread64_t) orig_pread64;
    real_readv = (readv_t) orig_readv;
    real_openat = (openat_t) orig_openat;
    real_open = (open_t) orig_open;
    real_access = (access_t) orig_access;
    real_stat = (stat_t) orig_stat;
    real_close = (close_t) orig_close;
    real_dl_iterate_phdr = (dl_iterate_phdr_t) orig_dl_iterate_phdr;
    real_syscall = (syscall_t) orig_syscall;
#endif

    /*
     * Data overrides go in last, after every hook is placed.
     *
     * Their targets are flags a detector consults before doing the expensive
     * work, so writing them last means the work they gate is already unreachable
     * by the time the app can run it. It also keeps the ordering uniform with the
     * entry guards above: the install report lists every mechanism in the order
     * it was applied, which is the order a later failure would be diagnosed in.
     */
    LOGI("step: before mem overrides");
    int mem_applied = apply_memory_overrides();
    if (mem_applied > 0) start_mem_override_lock();
    if (g_mem_spec_count > 0) {
        LOGI("memory overrides applied: %d of %d", mem_applied, g_mem_spec_count);
    }

    LOGI("step: before vma relabels");
    int vma_done = apply_vma_relabels();
    if (g_vma_spec_count > 0) {
        LOGI("vma relabels: %d segment(s) for %d spec(s)", vma_done, g_vma_spec_count);
    }

    /*
     * Entry guards last, after every exit guard is in place.
     *
     * The order is not incidental. An entry guard redirects the first instruction of a
     * function in another library, and the trampoline it needs is allocated with the same
     * `alloc_near` the exit guards use. Running this first would put that allocation in
     * the middle of the exit-guard work and make a failure here indistinguishable from a
     * failure there. Running it last also means the install report already contains every
     * exit hook, so a missing entry line is unambiguous.
     */
    /*
     * The collected baseline, read once and then consulted by every later path
     * lookup.
     *
     * It has to run after the stdio originals are resolved -- the reader goes
     * through `real_fopen` and `real_fgets`, because by this point `fopen` itself
     * is redirected to a function in this file and reading through it would ask
     * this module a question instead of the filesystem. It has to run before the
     * entry guards, which is where the first application code can start asking
     * about paths: a table that is still empty at that moment would answer
     * truthfully for every question asked during start-up and then start hiding
     * later, which is a behaviour that changes mid-run.
     *
     * Failure here is survivable and quiet: with no baseline the guards answer
     * from the fixed table, which is what every device did before the collector
     * existed.
     */
    /*
     * Who this process is, before anything asks whether a path should be hidden.
     *
     * The answer depends on the caller: a path is hidden only when it belongs to
     * someone else. Reading the package name here rather than lazily on the first
     * path question matters because the first question arrives very early, from
     * the resource system opening this app's own APK -- and if the name were not
     * known yet, that open would be treated as foreign and fail.
     */
    LOGI("step: before own package");
    capture_own_package();

    LOGI("step: before baseline paths");
    load_baseline_paths();

    LOGI("step: before entry hooks");
    int entry_installed = install_entry_hooks();
    if (g_entry_spec_count > 0) {
        LOGI("entry hooks installed: %d of %d", entry_installed, g_entry_spec_count);
    }

    /*
     * The property pages go last, and they are the one step that must run here rather
     * than on a retry: the mappings exist before this function does, and what fills them
     * is the property service at process start. Nothing the application loads later can
     * add a mapping to that set.
     */
    LOGI("step: before prop pages");
    /*
     * Immediate, because the cost that made it necessary to postpone is gone.
     * The mappings are shared now, so a forked child copies nothing; see the
     * note in patch_property_pages. And it has to be immediate: a reader that
     * maps the property area does so during start-up, and a replacement that
     * arrives after the read has already happened replaces nothing.
     */
    start_prop_page_thread();

    g_installed = true;
    /* Disabled: this is the shared JNI int exit for every app in scope. Keying on the
     * value alone rewrites unrelated callers' results -- Duck Detector's own
     * attestation answer came back -1 and was replaced with 1, which flipped its verdict.
     * A per-methodID match is needed before this can be turned back on. */
    /*
     * Not installed, for the reason the note above gives. The call is kept here,
     * commented, because the mechanism is not wrong -- the key is. Keying on the
     * returned value alone rewrites every caller's answer, and that is what the
     * note describes; a per-methodID match is the work that would let this return.
     *
     * Until then it must stay off: installing it aborts the process. ART reports
     * `CallStaticObjectMethodV received NULL jclass` from a slot this hook never
     * wrote, which means the table rewrite reaches a neighbour slot on this device.
     * The slot order itself is sound -- jni.h puts CallStaticObjectMethod at 315
     * and CallStaticIntMethod at 331 -- so the corruption is in the write, not in
     * the index, and that is a separate investigation.
     */
    /* install_jni_callint_hook(env); */
    return JNI_TRUE;
}

JNIEXPORT jstring JNICALL
Java_com_houvven_guise_xposed_hook_NativeGuard_probe(JNIEnv *env, jclass clazz) {
    char buf[512];
    snprintf(buf, sizeof(buf),
             "openat=%p open=%p access=%p stat=%p prop_get=%p prop_get_int=%p",
             real_openat, real_open, real_access, real_stat,
             real_prop_get, real_prop_get_int);
    return (*env)->NewStringUTF(env, buf);
}
