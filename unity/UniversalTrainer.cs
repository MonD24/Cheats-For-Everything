// UniversalTrainer: one BepInEx plugin for any Unity (Mono) game.
// Cheats come from BepInEx/config/universal_trainer.txt, one per line:
//   name | hotkey | class | member | mode | value [| check | then: Class Method to call afterwards]
//   Infinite Health | F1 | Game.Player | _health | lock | 0
//   God Mode        | F2 | Game.Player | TakeDamage | skip | Game.Player
//   +1000 Money     | F3 | PersistentData | Money | add | 1000
// mode: freeze (keep value while ON), lock (keep the value it had when switched ON),
//       set (once), add (once), multiply (once),
//       skip / return_true / return_false: Harmony-patch METHOD `member` of `class` while ON
//       (value = class whose instances are affected, default `class`; e.g. skip Player.TakeDamage).
// member may be a path through object references: Stats.Gold, mInventory.Coins.
// value "@Member" = read from another member,
//       "@x0.5" = half of the value it had when switched on. Vector members take "x,y[,z]".
// class: objects of this type (or subclasses) found in the scene, or the type's static member.
//        member can be a field or property, public or private, declared on any base class.
// Automation channel (polled twice a second):
//   BepInEx/trainer_cmd.txt   one command per line: on <name> | off <name> | toggle <name> |
//                             dump | inspect <class> | get <class> <member> | set <class> <member> <value> | reload |
//                             snap <tag> | diff <a> <b> decreased|increased|changed|unchanged [filter] |
//                             find <tag> <value> [filter] | objects <class> | methods <class> [filter] | pos <class>
//   BepInEx/trainer_state.txt result of the last commands + every cheat's state and live value
// Overlay: shows the cheat list; Insert hides/shows it.
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using BepInEx;
using HarmonyLib;
using UnityEngine;

[BepInPlugin("dd.universaltrainer", "Universal Trainer", "1.0.0")]
public class UniversalTrainer : BaseUnityPlugin
{
    class Cheat
    {
        public string Name, Hotkey, Class, Member, Mode, Value, Then = "";
        public KeyCode Key; public int Vk; public bool Ctrl, Shift, Alt, WasDown;
        public bool On; public string Error = "";
        public Dictionary<object, object> Locked = new Dictionary<object, object>();
        // values before a modifier freeze (time scale, flags, multipliers) took over: put back when OFF
        public Dictionary<object, KeyValuePair<object, object>> Original = new Dictionary<object, KeyValuePair<object, object>>();
    }

    // A freeze of a constant / relative value is a modifier: switching it off restores the old value.
    // Bars filled from another member (@MaxHealth) and locks keep what the game has now.
    // "!value" = freeze without restoring (enemies at 1 HP must not heal back when switched off)
    static bool Restores(Cheat c) { return c.Mode == "freeze" && !c.Value.StartsWith("!") && (!c.Value.StartsWith("@") || c.Value.StartsWith("@x")); }

    static void Restore(Cheat c)
    {
        foreach (KeyValuePair<object, KeyValuePair<object, object>> kv in c.Original)
            try { ((Slot)kv.Value.Key).Set(kv.Value.Value); } catch { }
        c.Original.Clear();
    }

    const BindingFlags ALL = BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public |
                             BindingFlags.NonPublic | BindingFlags.FlattenHierarchy;
    List<Cheat> cheats = new List<Cheat>();
    Dictionary<string, Type> typeCache = new Dictionary<string, Type>();
    Dictionary<Type, UnityEngine.Object[]> objCache = new Dictionary<Type, UnityEngine.Object[]>();
    Dictionary<Type, float> objCacheTime = new Dictionary<Type, float>();
    float nextPoll;
    bool showOverlay = true;
    string cfgPath, cmdPath, statePath;
    StringBuilder lastOutput = new StringBuilder();

    void Awake()
    {
        cfgPath = Path.Combine(Paths.ConfigPath, "universal_trainer.txt");
        cmdPath = Path.Combine(Paths.BepInExRootPath, "trainer_cmd.txt");
        statePath = Path.Combine(Paths.BepInExRootPath, "trainer_state.txt");
        Load();
        Logger.LogInfo("Universal Trainer loaded " + cheats.Count + " cheats");
    }

    void Load()
    {
        cheats.Clear();
        if (!File.Exists(cfgPath)) { File.WriteAllText(cfgPath, "# name | hotkey | class | member | mode | value\n"); return; }
        foreach (string raw in File.ReadAllLines(cfgPath))
        {
            string line = raw.Trim();
            if (line.Length == 0 || line.StartsWith("#")) continue;
            string[] p = line.Split('|').Select(s => s.Trim()).ToArray();
            if (p.Length < 6) continue;
            Cheat c = new Cheat { Name = p[0], Hotkey = p[1], Class = p[2], Member = p[3], Mode = p[4].ToLower(), Value = p[5] };
            // column 7 = where the value is checked (tooling only); column 8 = "Class Method" to call after
            // the cheat writes, e.g. the game's HUD refresh when labels only update on events
            if (p.Length > 7) c.Then = p[7];
            foreach (string part in c.Hotkey.ToUpper().Split('+'))
            {
                if (part == "CTRL") c.Ctrl = true; else if (part == "SHIFT") c.Shift = true;
                else if (part == "ALT") c.Alt = true;
                else if (part.Length > 0) { c.Vk = VirtualKey(part); if (c.Vk == 0) c.Error = "bad hotkey"; }
            }
            cheats.Add(c);
        }
    }

    Type FindType(string name)
    {
        Type t;
        if (typeCache.TryGetValue(name, out t)) return t;
        foreach (Assembly a in AppDomain.CurrentDomain.GetAssemblies())
        {
            try { t = a.GetType(name, false) ?? a.GetTypes().FirstOrDefault(x => x.Name == name || x.FullName == name); }
            catch { t = null; }
            if (t != null) break;
        }
        typeCache[name] = t;
        return t;
    }

    MemberInfo FindMember(Type t, string member)
    {
        for (Type x = t; x != null; x = x.BaseType)
        {
            FieldInfo f = x.GetField(member, ALL | BindingFlags.DeclaredOnly);
            if (f != null) return f;
            PropertyInfo p = x.GetProperty(member, ALL | BindingFlags.DeclaredOnly);
            if (p != null) return p;
        }
        return null;
    }

    static bool IsStatic(MemberInfo m)
    {
        FieldInfo f = m as FieldInfo;
        if (f != null) return f.IsStatic;
        MethodInfo g = ((PropertyInfo)m).GetGetMethod(true);
        return g != null && g.IsStatic;
    }

    static Type MemberType(MemberInfo m) { FieldInfo f = m as FieldInfo; return f != null ? f.FieldType : ((PropertyInfo)m).PropertyType; }
    static object Get(MemberInfo m, object o) { FieldInfo f = m as FieldInfo; return f != null ? f.GetValue(o) : ((PropertyInfo)m).GetValue(o, null); }
    static void Set(MemberInfo m, object o, object v)
    {
        FieldInfo f = m as FieldInfo;
        if (f != null) f.SetValue(o, v);
        else { PropertyInfo p = (PropertyInfo)m; if (p.CanWrite) p.SetValue(o, v, null); else throw new Exception("read-only property"); }
    }

    // All live targets: null for static members, else scene objects of the type.
    IEnumerable<object> Targets(Type t, MemberInfo m)
    {
        if (IsStatic(m)) { yield return null; yield break; }
        if (!typeof(UnityEngine.Object).IsAssignableFrom(t))
        {
            // plain C# class: look for a static Instance / singleton
            foreach (string n in new[] { "Instance", "instance", "_instance", "Current", "current", "Singleton" })
            {
                MemberInfo s = FindMember(t, n);
                if (s != null && IsStatic(s)) { object o = Get(s, null); if (o != null) { yield return o; yield break; } }
            }
            yield break;
        }
        UnityEngine.Object[] objs; float stamp;
        // re-scan the scene for this type at most once a second (objects come and go with scenes)
        if (!objCache.TryGetValue(t, out objs) || !objCacheTime.TryGetValue(t, out stamp) || Time.unscaledTime - stamp > 1f)
        {
            objs = UnityEngine.Object.FindObjectsOfType(t);
            objCache[t] = objs;
            objCacheTime[t] = Time.unscaledTime;
        }
        foreach (UnityEngine.Object o in objs) if (o != null) yield return o;
    }

    object Convert(string v, Type to)
    {
        if (to == typeof(bool)) return v == "1" || v.ToLower() == "true";
        if (to == typeof(Vector2) || to == typeof(Vector3))
        {
            float[] f = v.Split(',').Select(x => float.Parse(x.Trim(), System.Globalization.CultureInfo.InvariantCulture)).ToArray();
            if (to == typeof(Vector2)) return new Vector2(f[0], f.Length > 1 ? f[1] : 0);
            return new Vector3(f[0], f.Length > 1 ? f[1] : 0, f.Length > 2 ? f[2] : 0);
        }
        if (to.IsEnum) return Enum.Parse(to, v, true);
        return System.Convert.ChangeType(v, to, System.Globalization.CultureInfo.InvariantCulture);
    }

    object Combine(object cur, string v, string mode)
    {
        double a = System.Convert.ToDouble(cur), b = double.Parse(v, System.Globalization.CultureInfo.InvariantCulture);
        double r = mode == "add" ? a + b : a * b;
        return System.Convert.ChangeType(r, cur.GetType());
    }


    // ---- member paths: "A.B[2].C" from a target object (or static type when root == null).
    // A Slot is the final storage place: a field/property, or one element of an array/list.
    class Slot
    {
        public object Owner; public MemberInfo M; public int Index = -1;
        public object Get() { object v = UniversalTrainer.Get(M, Owner); return Index < 0 ? v : ((System.Collections.IList)v)[Index]; }
        public void Set(object v) { if (Index < 0) UniversalTrainer.Set(M, Owner, v); else ((System.Collections.IList)UniversalTrainer.Get(M, Owner))[Index] = v; }
        public Type Type
        {
            get
            {
                Type t = MemberType(M);
                if (Index < 0) return t;
                if (t.IsArray) return t.GetElementType();
                return t.IsGenericType ? t.GetGenericArguments()[0] : typeof(object);
            }
        }
    }

    Slot Leaf(object root, Type t, string path)
    {
        object owner = root;
        string[] parts = path.Split('.');
        for (int i = 0; i < parts.Length; i++)
        {
            string name = parts[i]; int idx = -1;
            int br = name.IndexOf('[');
            if (br >= 0) { idx = int.Parse(name.Substring(br + 1, name.IndexOf(']') - br - 1)); name = name.Substring(0, br); }
            MemberInfo m = FindMember(owner == null ? t : owner.GetType(), name);
            if (m == null) return null;
            if (i == parts.Length - 1) return new Slot { Owner = owner, M = m, Index = idx };
            owner = Get(m, owner);
            if (idx >= 0 && owner != null) owner = ((System.Collections.IList)owner)[idx];
            if (owner == null || owner.GetType().IsValueType) return null; // a struct copy cannot be written back
        }
        return null;
    }

    static string Root(string path) { int i = path.IndexOfAny(new[] { '.', '[' }); return i < 0 ? path : path.Substring(0, i); }

    // ---- method hooks (Harmony): skip a method or force a bool result, only for instances of Filter
    class Hook { public Type Filter; public string Mode; public bool On; }
    static Dictionary<MethodBase, Hook> hooks = new Dictionary<MethodBase, Hook>();
    static Harmony harmony;

    static bool HookActive(object inst, MethodBase m, out Hook h)
    {
        if (!hooks.TryGetValue(m, out h) || !h.On) return false;
        return h.Filter == null || (inst != null && h.Filter.IsInstanceOfType(inst));
    }
    static bool PrefixInst(object __instance, MethodBase __originalMethod)
    {
        Hook h; return !HookActive(__instance, __originalMethod, out h);
    }
    static bool PrefixInstBool(object __instance, MethodBase __originalMethod, ref bool __result)
    {
        Hook h; if (!HookActive(__instance, __originalMethod, out h)) return true;
        __result = h.Mode == "return_true"; return false;
    }
    static bool PrefixStatic(MethodBase __originalMethod)
    {
        Hook h; return !(hooks.TryGetValue(__originalMethod, out h) && h.On);
    }
    static bool PrefixStaticBool(MethodBase __originalMethod, ref bool __result)
    {
        Hook h; if (!(hooks.TryGetValue(__originalMethod, out h) && h.On)) return true;
        __result = h.Mode == "return_true"; return false;
    }

    static bool IsHookMode(string mode) { return mode == "skip" || mode == "return_true" || mode == "return_false"; }

    List<MethodInfo> MethodsNamed(Type t, string name)
    {
        for (Type x = t; x != null; x = x.BaseType)
        {
            List<MethodInfo> ms = x.GetMethods(ALL | BindingFlags.DeclaredOnly)
                .Where(m => m.Name == name && !m.IsAbstract && !m.ContainsGenericParameters).ToList();
            if (ms.Count > 0) return ms;
        }
        return new List<MethodInfo>();
    }

    void SetHook(Cheat c, bool on)
    {
        Type t = FindType(c.Class);
        if (t == null) { c.Error = "class not found"; c.On = false; return; }
        Type filter = (c.Value.Length == 0 || c.Value == "0" || c.Value == "*") ? t : FindType(c.Value);
        List<MethodInfo> ms = MethodsNamed(t, c.Member);
        if (ms.Count == 0) { c.Error = "method not found"; c.On = false; return; }
        if (harmony == null) harmony = new Harmony("dd.universaltrainer");
        int n = 0;
        foreach (MethodInfo m in ms)
        {
            bool isBool = m.ReturnType == typeof(bool);
            if (c.Mode != "skip" && !isBool) continue;            // return_true/false only for bool methods
            if (c.Mode == "skip" && m.ReturnType != typeof(void) && !isBool) continue; // skipping would return default(T)
            Hook h;
            if (!hooks.TryGetValue(m, out h))
            {
                h = new Hook();
                string pre = (m.IsStatic ? "PrefixStatic" : "PrefixInst") + (isBool ? "Bool" : "");
                harmony.Patch(m, new HarmonyMethod(typeof(UniversalTrainer).GetMethod(pre, BindingFlags.Static | BindingFlags.NonPublic)));
                hooks[m] = h;
            }
            h.Filter = m.IsStatic ? null : filter;
            h.Mode = (c.Mode == "skip" && isBool) ? "return_false" : c.Mode;
            h.On = on; n++;
        }
        c.On = on && n > 0;
        c.Error = n == 0 ? "no patchable overload (skip: void/bool methods; return_*: bool methods)" : "";
    }

    // ---- snapshots: every numeric field of every live game object, to diff like a memory scanner
    class Snap { public Dictionary<string, double> V = new Dictionary<string, double>(); public Dictionary<string, string> T = new Dictionary<string, string>(); }
    Dictionary<string, Snap> snaps = new Dictionary<string, Snap>();
    List<Type> gameTypes;

    static bool IsGameAssembly(Assembly a)
    {
        string n = a.GetName().Name;
        string[] skip = { "Unity", "System", "mscorlib", "Mono.", "BepInEx", "0Harmony", "Harmony", "UniversalTrainer",
                          "Rewired", "DOTween", "DemiLib", "TextMeshPro", "Newtonsoft", "netstandard", "Microsoft", "Sirenix",
                          "Cinemachine", "Steamworks", "Facepunch", "FMOD", "Wwise", "AK.", "Photon", "MonoMod", "HarmonyX",
                          "Google.", "Mirror", "FishNet", "Zenject", "UniRx", "UniTask", "Sentry", "Amplitude", "Unity.", "com.", "Discord" };
        foreach (string p in skip) if (n.StartsWith(p, StringComparison.OrdinalIgnoreCase)) return false;
        return true;
    }

    List<Type> GameTypes()
    {
        if (gameTypes != null) return gameTypes;
        gameTypes = new List<Type>();
        foreach (Assembly a in AppDomain.CurrentDomain.GetAssemblies())
        {
            if (!IsGameAssembly(a)) continue;
            Type[] ts;
            try { ts = a.GetTypes(); }
            catch (ReflectionTypeLoadException e) { ts = e.Types.Where(x => x != null).ToArray(); }
            catch { continue; }
            gameTypes.AddRange(ts.Where(x => x != null && !x.ContainsGenericParameters));
        }
        return gameTypes;
    }

    static string Pretty(string field)
    {
        int k = field.IndexOf(">k__BackingField");
        return field.StartsWith("<") && k > 0 ? field.Substring(1, k - 1) : field;
    }

    static bool Number(object v, out double d)
    {
        d = 0;
        if (v == null || v is Enum) return false;
        if (v is bool) { d = (bool)v ? 1 : 0; return true; }
        switch (Type.GetTypeCode(v.GetType()))
        {
            case TypeCode.Byte: case TypeCode.SByte: case TypeCode.Int16: case TypeCode.UInt16: case TypeCode.Int32:
            case TypeCode.UInt32: case TypeCode.Int64: case TypeCode.UInt64: case TypeCode.Single: case TypeCode.Double: case TypeCode.Decimal:
                d = System.Convert.ToDouble(v); return !double.IsNaN(d) && !double.IsInfinity(d);
        }
        return false;
    }

    static bool Walkable(object v, Type declared)
    {
        return v != null && declared.IsClass && !(v is UnityEngine.Object) && !(v is string) &&
               !(v is System.Collections.IEnumerable) && !(v is Delegate) && IsGameAssembly(v.GetType().Assembly);
    }

    void Collect(object o, Type t, string prefix, string key, Snap s, int depth)
    {
        for (Type x = t; x != null && x != typeof(MonoBehaviour) && x != typeof(Behaviour) && x != typeof(Component) &&
                         x != typeof(UnityEngine.Object) && x != typeof(ScriptableObject) && x != typeof(object); x = x.BaseType)
            foreach (FieldInfo f in x.GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly))
            {
                if (s.V.Count > 400000) return;
                object v; try { v = f.GetValue(o); } catch { continue; }
                double d;
                if (f.FieldType.IsPrimitive) { if (Number(v, out d)) { string k = key + "|" + prefix + Pretty(f.Name); s.V[k] = d; s.T[k] = f.FieldType.Name; } }
                else if (Elements(v, key + "|" + prefix + Pretty(f.Name), s)) { }
                else if (depth > 0 && v is System.Collections.IList && ((System.Collections.IList)v).Count <= 16)
                {
                    System.Collections.IList list = (System.Collections.IList)v;
                    for (int i = 0; i < list.Count; i++)
                        if (Walkable(list[i], typeof(object))) Collect(list[i], list[i].GetType(), prefix + Pretty(f.Name) + "[" + i + "].", key, s, depth - 1);
                }
                else if (depth > 0 && Walkable(v, f.FieldType)) Collect(v, v.GetType(), prefix + Pretty(f.Name) + ".", key, s, depth - 1);
            }
    }

    // small arrays / lists of numbers (per-character stats, inventories): one value per element
    static bool Elements(object v, string key, Snap s)
    {
        System.Collections.IList list = v as System.Collections.IList;
        if (list == null || list.Count == 0 || list.Count > 64) return false;
        Type et = v.GetType().IsArray ? v.GetType().GetElementType() : (v.GetType().IsGenericType ? v.GetType().GetGenericArguments()[0] : null);
        if (et == null || !et.IsPrimitive) return false;
        for (int i = 0; i < list.Count; i++) { double d; if (Number(list[i], out d)) { s.V[key + "[" + i + "]"] = d; s.T[key + "[" + i + "]"] = et.Name; } }
        return true;
    }

    Snap TakeSnap()
    {
        Snap s = new Snap();
        foreach (MonoBehaviour mb in UnityEngine.Object.FindObjectsOfType<MonoBehaviour>())
        {
            if (mb == null) continue;
            Type t = mb.GetType();
            if (!IsGameAssembly(t.Assembly)) continue;
            Collect(mb, t, "", t.FullName + "#" + mb.GetInstanceID(), s, 1);
        }
        foreach (Type t in GameTypes())   // statics: counters, settings, singletons of plain classes
        {
            FieldInfo[] fs;
            try { fs = t.GetFields(BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly); } catch { continue; }
            foreach (FieldInfo f in fs)
            {
                if (f.IsLiteral) continue;
                object v; try { v = f.GetValue(null); } catch { continue; }
                double d;
                if (f.FieldType.IsPrimitive) { if (Number(v, out d)) { string k = t.FullName + "#static|" + Pretty(f.Name); s.V[k] = d; s.T[k] = f.FieldType.Name; } }
                else if (Walkable(v, f.FieldType)) Collect(v, v.GetType(), Pretty(f.Name) + ".", t.FullName + "#static", s, 1);
            }
        }
        return s;
    }

    static string Fmt(double d) { return d.ToString("R", System.Globalization.CultureInfo.InvariantCulture); }

    void Diff(string arg)
    {
        string[] a = arg.Split(' ');
        Snap x, y;
        if (a.Length < 3 || !snaps.TryGetValue(a[0], out x) || !snaps.TryGetValue(a[1], out y))
        {
            lastOutput.AppendLine("diff -> usage: diff <a> <b> <decreased|increased|changed|unchanged> [filter]; snaps: " + string.Join(",", snaps.Keys.ToArray()));
            return;
        }
        string cond = a[2].ToLower(), filter = a.Length > 3 ? a[3] : "";
        int n = 0;
        foreach (KeyValuePair<string, double> kv in y.V)
        {
            double before;
            if (!x.V.TryGetValue(kv.Key, out before)) continue;
            if (!Matches(kv.Key, filter)) continue;
            double after = kv.Value;
            bool ok = cond == "decreased" ? after < before : cond == "increased" ? after > before :
                      cond == "changed" ? after != before : cond == "unchanged" && after == before;
            if (!ok) continue;
            if (n++ < 1500) lastOutput.AppendLine("d " + kv.Key + " " + y.T[kv.Key] + " " + Fmt(before) + " " + Fmt(after));
        }
        lastOutput.AppendLine("diff " + a[0] + " " + a[1] + " " + cond + " -> " + n + " matches");
    }

    // filter: substrings separated by '|', any of them may match; empty = everything
    static bool Matches(string key, string filter)
    {
        if (filter.Length == 0) return true;
        foreach (string f in filter.Split('|')) if (f.Length > 0 && key.IndexOf(f, StringComparison.OrdinalIgnoreCase) >= 0) return true;
        return false;
    }

    void Find(string arg)
    {
        string[] a = arg.Split(' ');
        Snap s;
        if (a.Length < 2 || !snaps.TryGetValue(a[0], out s)) { lastOutput.AppendLine("find -> usage: find <tag> <value> [filter]"); return; }
        double want = double.Parse(a[1], System.Globalization.CultureInfo.InvariantCulture);
        string filter = a.Length > 2 ? a[2] : "";
        int n = 0;
        foreach (KeyValuePair<string, double> kv in s.V)
        {
            if (Math.Abs(kv.Value - want) > 0.5 || s.T[kv.Key] == "Boolean") continue;
            if (!Matches(kv.Key, filter)) continue;
            if (n++ < 1500) lastOutput.AppendLine("f " + kv.Key + " " + s.T[kv.Key] + " " + Fmt(kv.Value));
        }
        lastOutput.AppendLine("find " + a[1] + " -> " + n + " matches");
    }

    // positions of every active game MonoBehaviour: whatever moves when a movement key is held is the player
    Dictionary<string, Dictionary<string, Vector3>> posSnaps = new Dictionary<string, Dictionary<string, Vector3>>();

    Dictionary<string, Vector3> PosSnap()
    {
        Dictionary<string, Vector3> d = new Dictionary<string, Vector3>();
        foreach (MonoBehaviour mb in UnityEngine.Object.FindObjectsOfType<MonoBehaviour>())
        {
            if (mb == null || !mb.isActiveAndEnabled || !IsGameAssembly(mb.GetType().Assembly)) continue;
            d[mb.GetType().FullName + "#" + mb.GetInstanceID()] = mb.transform.position;
        }
        return d;
    }

    void PosDiff(string arg)
    {
        string[] a = arg.Split(' ');
        Dictionary<string, Vector3> x, y;
        if (a.Length < 2 || !posSnaps.TryGetValue(a[0], out x) || !posSnaps.TryGetValue(a[1], out y)) { lastOutput.AppendLine("posdiff -> usage: posdiff <a> <b>"); return; }
        List<KeyValuePair<string, float>> moved = new List<KeyValuePair<string, float>>();
        foreach (KeyValuePair<string, Vector3> kv in y)
        {
            Vector3 before;
            if (x.TryGetValue(kv.Key, out before)) { float d = Vector3.Distance(before, kv.Value); if (d > 0.001f) moved.Add(new KeyValuePair<string, float>(kv.Key, d)); }
        }
        foreach (KeyValuePair<string, float> kv in moved.OrderByDescending(m => m.Value).Take(40))
            lastOutput.AppendLine("moved " + kv.Key + " " + kv.Value.ToString("R", System.Globalization.CultureInfo.InvariantCulture));
        lastOutput.AppendLine("posdiff -> " + moved.Count + " moved");
    }

    void Objects(string cls)
    {
        Type t = FindType(cls);
        if (t == null || !typeof(UnityEngine.Object).IsAssignableFrom(t)) { lastOutput.AppendLine("objects " + cls + " -> class not found or not a Unity object"); return; }
        UnityEngine.Object[] objs = UnityEngine.Object.FindObjectsOfType(t);
        foreach (UnityEngine.Object o in objs.Take(50))
        {
            Component c = o as Component;
            lastOutput.AppendLine("obj " + o.GetInstanceID() + " " + o.GetType().FullName + " name=" + o.name +
                (c != null ? " active=" + c.gameObject.activeInHierarchy : ""));
        }
        lastOutput.AppendLine("objects " + cls + " -> " + objs.Length);
    }

    void Methods(string arg)
    {
        string[] a = arg.Split(' ');
        Type t = FindType(a[0]);
        if (t == null) { lastOutput.AppendLine("methods -> class not found"); return; }
        string filter = a.Length > 1 ? a[1] : "";
        for (Type x = t; x != null && x != typeof(MonoBehaviour) && x != typeof(object); x = x.BaseType)
            foreach (MethodInfo m in x.GetMethods(ALL | BindingFlags.DeclaredOnly))
            {
                if (m.Name.StartsWith("get_") || m.Name.StartsWith("set_") || m.Name.StartsWith("<")) continue;
                if (filter.Length > 0 && m.Name.IndexOf(filter, StringComparison.OrdinalIgnoreCase) < 0) continue;
                lastOutput.AppendLine("method " + x.FullName + "." + m.Name + "(" +
                    string.Join(", ", m.GetParameters().Select(p => p.ParameterType.Name).ToArray()) + ") : " +
                    m.ReturnType.Name + (m.IsStatic ? " static" : ""));
            }
    }

    void Apply(Cheat c, bool once)
    {
        if (IsHookMode(c.Mode)) return;   // hooks act inside the patched method, nothing to write per frame
        try
        {
            Type t = FindType(c.Class);
            if (t == null) { c.Error = "class not found"; return; }
            MemberInfo rootMember = FindMember(t, Root(c.Member));
            if (rootMember == null) { c.Error = "member not found"; return; }
            int n = 0;
            foreach (object o in Targets(t, rootMember))
            {
                Slot slot = Leaf(o, t, c.Member);
                if (slot == null) continue;
                object owner = slot.Owner;
                object key = (object)owner ?? t;
                if (slot.Index >= 0) key = new KeyValuePair<object, int>(key, slot.Index);
                if (Restores(c) && !c.Original.ContainsKey(key)) c.Original[key] = new KeyValuePair<object, object>(slot, slot.Get());
                // value "@OtherMember" = take it from another member of the same object (e.g. @MaxSpecial)
                string raw = c.Value.TrimStart('!');
                if (raw.StartsWith("@x"))
                {
                    // relative: freeze at (value when switched on) * factor
                    if (!c.Locked.ContainsKey(key)) c.Locked[key] = Combine(slot.Get(), raw.Substring(2), "multiply");
                    slot.Set(c.Locked[key]); n++; continue;
                }
                if (raw.StartsWith("@"))
                {
                    MemberInfo src = FindMember(owner == null ? t : owner.GetType(), raw.Substring(1));
                    if (src == null) { c.Error = "member " + raw + " not found"; return; }
                    raw = System.Convert.ToString(Get(src, owner), System.Globalization.CultureInfo.InvariantCulture);
                }
                object v;
                if (c.Mode == "lock")
                {
                    // remember the value the moment the cheat is switched on, then hold it
                    if (!c.Locked.ContainsKey(key)) c.Locked[key] = slot.Get();
                    v = c.Locked[key];
                }
                else v = (c.Mode == "add" || c.Mode == "multiply") ? Combine(slot.Get(), raw, c.Mode) : Convert(raw, slot.Type);
                slot.Set(v); n++;
            }
            c.Error = n == 0 ? "no live object yet" : "";
        }
        catch (Exception e) { c.Error = e.GetType().Name + ": " + e.Message; }
    }

    string Read(Cheat c)
    {
        if (IsHookMode(c.Mode)) return c.On ? "hooked" : "-";
        try
        {
            Type t = FindType(c.Class); MemberInfo m = t == null ? null : FindMember(t, Root(c.Member));
            if (m == null) return "?";
            foreach (object o in Targets(t, m))
            {
                Slot slot = Leaf(o, t, c.Member);
                if (slot != null) return System.Convert.ToString(slot.Get(), System.Globalization.CultureInfo.InvariantCulture);
            }
            return "-";
        }
        catch (Exception e) { return "err " + e.Message; }
    }

    void RunThen(Cheat c)
    {
        if (c.Then.Length == 0) return;
        StringBuilder keep = new StringBuilder(lastOutput.ToString());
        try { Call(c.Then); } catch (Exception e) { c.Error = "then: " + e.Message; }
        lastOutput.Length = 0; lastOutput.Append(keep.ToString());
    }

    void SwitchOn(Cheat c, bool on)
    {
        if (IsHookMode(c.Mode)) { try { SetHook(c, on); } catch (Exception e) { c.On = false; c.Error = e.GetType().Name + ": " + e.Message; } return; }
        if (c.Mode == "freeze" || c.Mode == "lock")
        {
            if (!on && c.On) Restore(c);
            c.On = on; c.Locked.Clear();
            if (on) { Apply(c, false); RunThen(c); }
        }
        else if (on) { Apply(c, true); RunThen(c); }
    }

    void Press(Cheat c)
    {
        bool toggles = c.Mode == "freeze" || c.Mode == "lock" || IsHookMode(c.Mode);
        SwitchOn(c, toggles ? !c.On : true);
    }

    // Hotkeys through Win32, not UnityEngine.Input: games on the new Input System throw on every legacy
    // Input call (that exception used to kill Update before the command channel was polled).
    [System.Runtime.InteropServices.DllImport("user32.dll")] static extern short GetAsyncKeyState(int vKey);
    [System.Runtime.InteropServices.DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [System.Runtime.InteropServices.DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    static readonly uint myPid = (uint)System.Diagnostics.Process.GetCurrentProcess().Id;
    bool insertWasDown;

    static int VirtualKey(string k)
    {
        if (k.StartsWith("F") && k.Length > 1) { int n; if (int.TryParse(k.Substring(1), out n) && n >= 1 && n <= 24) return 0x6F + n; }
        if (k.StartsWith("NUM")) { int n; if (int.TryParse(k.Substring(3), out n) && n >= 0 && n <= 9) return 0x60 + n; }
        if (k.Length == 1 && ((k[0] >= 'A' && k[0] <= 'Z') || (k[0] >= '0' && k[0] <= '9'))) return k[0];
        switch (k) { case "INSERT": return 0x2D; case "DELETE": return 0x2E; case "HOME": return 0x24; case "END": return 0x23;
                     case "PGUP": return 0x21; case "PGDN": return 0x22; }
        return 0;
    }

    static bool Down(int vk) { return (GetAsyncKeyState(vk) & 0x8000) != 0; }

    static bool GameFocused()
    {
        uint pid; GetWindowThreadProcessId(GetForegroundWindow(), out pid);
        return pid == myPid;
    }

    void Update()
    {
        if (Time.unscaledTime > nextPoll)
        {
            nextPoll = Time.unscaledTime + 0.5f;
            try { Poll(); } catch (Exception e) { Logger.LogWarning("poll: " + e.Message); }
        }
        try
        {
            bool focused = GameFocused();
            bool ins = focused && Down(0x2D);
            if (ins && !insertWasDown) showOverlay = !showOverlay;
            insertWasDown = ins;
            bool ctrl = Down(0x11), shift = Down(0x10), alt = Down(0x12);
            foreach (Cheat c in cheats)
            {
                bool down = focused && c.Vk != 0 && Down(c.Vk);
                if (down && !c.WasDown && c.Ctrl == ctrl && c.Shift == shift && c.Alt == alt) Press(c);
                c.WasDown = down;
            }
        }
        catch (Exception e) { Logger.LogWarning("hotkeys: " + e.Message); }
        foreach (Cheat c in cheats) if (c.On) Apply(c, false);
    }

    Cheat ByName(string n) { return cheats.FirstOrDefault(c => c.Name.Equals(n.Trim(), StringComparison.OrdinalIgnoreCase)); }

    void Poll()
    {
        if (!File.Exists(cmdPath)) return;
        string[] lines;
        try { lines = File.ReadAllLines(cmdPath); File.Delete(cmdPath); } catch { return; }
        lastOutput.Length = 0;
        foreach (string raw in lines)
        {
            string line = raw.Trim(); if (line.Length == 0) continue;
            string[] p = line.Split(new[] { ' ' }, 2);
            string verb = p[0].ToLower(), arg = p.Length > 1 ? p[1] : "";
            try
            {
                Cheat c;
                switch (verb)
                {
                    case "on": c = ByName(arg); if (c != null) SwitchOn(c, true); lastOutput.AppendLine(line + " -> " + (c == null ? "no such cheat" : (c.Error.Length > 0 ? "error " + c.Error : "ok"))); break;
                    case "off": c = ByName(arg); if (c != null) { SwitchOn(c, false); } lastOutput.AppendLine(line + " -> " + (c == null ? "no such cheat" : "ok")); break;
                    case "toggle": c = ByName(arg); if (c != null) Press(c); lastOutput.AppendLine(line + " -> ok"); break;
                    case "reload": Load(); lastOutput.AppendLine("reloaded " + cheats.Count); break;
                    case "inspect": Inspect(arg); break;
                    case "get":
                    {
                        string[] a = arg.Split(' '); Type t = FindType(a[0]);
                        MemberInfo m = t == null ? null : FindMember(t, Root(a[1]));
                        if (m == null) { lastOutput.AppendLine(line + " -> not found"); break; }
                        int i = 0; foreach (object o in Targets(t, m)) {
                            Slot slot = Leaf(o, t, a[1]);
                            if (slot == null) { lastOutput.AppendLine(line + " -> path not resolved"); i++; break; }
                            object v = slot.Get();
                            System.Collections.IList list = v as System.Collections.IList;
                            string shown = list != null ? "[" + string.Join(", ", list.Cast<object>().Select(x => System.Convert.ToString(x, System.Globalization.CultureInfo.InvariantCulture)).ToArray()) + "]" : System.Convert.ToString(v, System.Globalization.CultureInfo.InvariantCulture);
                            lastOutput.AppendLine(line + " [" + i + "] = " + shown); if (++i >= 4) break; }
                        if (i == 0) lastOutput.AppendLine(line + " -> no live object");
                        break;
                    }
                    case "set":
                    {
                        string[] a = arg.Split(' '); Type t = FindType(a[0]); MemberInfo m = t == null ? null : FindMember(t, Root(a[1]));
                        int i = 0;
                        if (m != null) foreach (object o in Targets(t, m))
                        {
                            Slot slot = Leaf(o, t, a[1]);
                            if (slot != null) { slot.Set(Convert(a[2], slot.Type)); i++; }
                        }
                        lastOutput.AppendLine(line + " -> set on " + i); break;
                    }
                    case "dump": break;
                    case "keymap": KeyMap(); break;
                    case "call": Call(arg); break;
                    case "types": Types(arg); break;
                    case "snap": snaps[arg.Trim().Length > 0 ? arg.Trim() : "last"] = TakeSnap(); lastOutput.AppendLine("snap " + arg + " -> " + snaps[arg.Trim().Length > 0 ? arg.Trim() : "last"].V.Count + " values"); break;
                    case "diff": Diff(arg); break;
                    case "find": Find(arg); break;
                    case "objects": Objects(arg.Trim()); break;
                    case "methods": Methods(arg.Trim()); break;
                    case "possnap": posSnaps[arg.Trim()] = PosSnap(); lastOutput.AppendLine("possnap " + arg + " -> " + posSnaps[arg.Trim()].Count); break;
                    case "posdiff": PosDiff(arg); break;
                    case "pos":
                    {
                        Type t = FindType(arg.Trim());
                        Component o = t == null || !typeof(Component).IsAssignableFrom(t) ? null : UnityEngine.Object.FindObjectsOfType(t).Cast<Component>().FirstOrDefault(x => x.gameObject.activeInHierarchy);
                        if (o == null) { lastOutput.AppendLine("pos -> no live object"); break; }
                        Vector3 v = o.transform.position;
                        lastOutput.AppendLine("pos " + v.x.ToString("R", System.Globalization.CultureInfo.InvariantCulture) + " " + v.y.ToString("R", System.Globalization.CultureInfo.InvariantCulture) + " " + v.z.ToString("R", System.Globalization.CultureInfo.InvariantCulture));
                        break;
                    }
                    default: lastOutput.AppendLine(line + " -> unknown command"); break;
                }
            }
            catch (Exception e) { lastOutput.AppendLine(line + " -> ERROR " + e.Message); }
        }
        WriteState();
    }

    // Keyboard bindings of games that use Rewired (very common in Unity), via reflection so the
    // plugin still loads in games without it.
    void KeyMap()
    {
        Type re = FindType("Rewired.ReInput");
        if (re == null) { if (!InputSystemMap()) lastOutput.AppendLine("keymap -> no Rewired / Input System bindings found (try arrows/WASD, Enter/Space, Esc)"); return; }
        object players = Val(null, re, "players");
        object mapping = Val(null, re, "mapping");
        Type ctype = FindType("Rewired.ControllerType");
        object keyboard = Enum.Parse(ctype, "Keyboard");
        foreach (object player in ((System.Collections.IEnumerable)Val(players, players.GetType(), "AllPlayers")).Cast<object>())
        {
            object maps = Val(Val(player, player.GetType(), "controllers"), null, "maps");
            MethodInfo getAll = maps.GetType().GetMethods().First(m => m.Name == "GetAllMaps" && m.GetParameters().Length == 1 && m.GetParameters()[0].ParameterType == ctype);
            foreach (object map in (System.Collections.IEnumerable)getAll.Invoke(maps, new object[] { keyboard }))
            {
                bool enabled = (bool)Val(map, null, "enabled");
                foreach (object aem in (System.Collections.IEnumerable)Val(map, null, "AllMaps"))
                {
                    int actionId = (int)Val(aem, null, "actionId");
                    object action = mapping == null ? null : mapping.GetType().GetMethod("GetAction", new Type[] { typeof(int) }).Invoke(mapping, new object[] { actionId });
                    string aname = action == null ? actionId.ToString() : (Val(action, null, "descriptiveName") ?? Val(action, null, "name")).ToString();
                    lastOutput.AppendLine("key player=" + Val(player, null, "name") + (enabled ? "" : " (disabled)")
                        + " action=" + aname + " key=" + Val(aem, null, "keyCode") + " (" + Val(aem, null, "elementIdentifierName") + ")");
                }
            }
        }
    }

    // Unity's new Input System: keyboard bindings of every InputActionAsset, read by reflection so the plugin
    // still loads in games without the package. Composite parts come out as map.action.part (Move.up).
    bool InputSystemMap()
    {
        Type assetType = FindType("UnityEngine.InputSystem.InputActionAsset");
        if (assetType == null) return false;
        int n = 0;
        foreach (UnityEngine.Object asset in Resources.FindObjectsOfTypeAll(assetType))
            foreach (object map in (System.Collections.IEnumerable)Val(asset, null, "actionMaps"))
                foreach (object action in (System.Collections.IEnumerable)Val(map, null, "actions"))
                    foreach (object b in (System.Collections.IEnumerable)Val(action, null, "bindings"))
                    {
                        string path = System.Convert.ToString(Val(b, null, "effectivePath") ?? Val(b, null, "path"));
                        if (path == null || !path.StartsWith("<Keyboard>/")) continue;
                        bool part = (bool)Val(b, null, "isPartOfComposite");
                        string name = Val(map, null, "name") + "." + Val(action, null, "name") + (part ? "." + Val(b, null, "name") : "");
                        lastOutput.AppendLine("key player=Player0 action=" + name.Replace(" ", "") + " key=" + path.Substring(11));
                        n++;
                    }
        return n > 0;
    }

    // property or field, public or not; t defaults to o's type
    static object Val(object o, Type t, string name)
    {
        t = t ?? o.GetType();
        PropertyInfo p = t.GetProperty(name, ALL);
        if (p != null) return p.GetValue(o, null);
        FieldInfo f = t.GetField(name, ALL);
        return f != null ? f.GetValue(o) : null;
    }

    // call <Class> <Method> [args...]  -> static, or on the first live object
    void Call(string arg)
    {
        string[] a = arg.Split(' ');
        Type t = FindType(a[0]);
        if (t == null) { lastOutput.AppendLine("call -> class not found"); return; }
        string[] args = a.Skip(2).ToArray();
        MethodInfo m = null;
        for (Type x = t; x != null && m == null; x = x.BaseType)
            m = x.GetMethods(ALL | BindingFlags.DeclaredOnly).FirstOrDefault(mm => mm.Name == a[1] && mm.GetParameters().Length == args.Length);
        if (m == null) { lastOutput.AppendLine("call -> method " + a[1] + "/" + args.Length + " not found"); return; }
        object[] conv = m.GetParameters().Select((p, i) => Convert(args[i], p.ParameterType)).ToArray();
        object target = null;
        if (!m.IsStatic)
        {
            UnityEngine.Object[] objs = typeof(UnityEngine.Object).IsAssignableFrom(t) ? UnityEngine.Object.FindObjectsOfType(t) : new UnityEngine.Object[0];
            if (objs.Length == 0) { lastOutput.AppendLine("call -> no live object of " + t.Name); return; }
            target = objs[0];
        }
        object r = m.Invoke(target, conv);
        lastOutput.AppendLine("call " + t.Name + "." + m.Name + " -> " + (r ?? "ok"));
    }

    // types <substring> : list game classes whose name contains it (live object count)
    void Types(string sub)
    {
        foreach (Assembly asm in AppDomain.CurrentDomain.GetAssemblies())
        {
            if (!asm.GetName().Name.StartsWith("Assembly-CSharp")) continue;
            Type[] ts; try { ts = asm.GetTypes(); } catch (ReflectionTypeLoadException e) { ts = e.Types.Where(x => x != null).ToArray(); }
            foreach (Type t in ts)
                if (t.FullName.IndexOf(sub, StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    int n = typeof(UnityEngine.Object).IsAssignableFrom(t) && !t.IsAbstract && !t.ContainsGenericParameters ? UnityEngine.Object.FindObjectsOfType(t).Length : -1;
                    lastOutput.AppendLine("type " + t.FullName + (n >= 0 ? " live=" + n : ""));
                }
        }
    }

    // Follow "A.B[2].C" from a value; returns the final value (read only).
    object Walk(object o, string path)
    {
        foreach (string part0 in path.Split('.'))
        {
            if (o == null) return null;
            string part = part0; int idx = -1;
            int b = part.IndexOf('[');
            if (b >= 0) { idx = int.Parse(part.Substring(b + 1, part.IndexOf(']') - b - 1)); part = part.Substring(0, b); }
            MemberInfo m = FindMember(o.GetType(), part);
            if (m == null) throw new Exception("no member " + part + " on " + o.GetType().Name);
            o = Get(m, o);
            if (idx >= 0) o = ((System.Collections.IList)o)[idx];
        }
        return o;
    }

    void Inspect(string cls)
    {
        Type t = FindType(cls);
        if (t == null) { lastOutput.AppendLine("inspect " + cls + " -> class not found"); return; }
        object target = null; int count = 0;
        if (typeof(UnityEngine.Object).IsAssignableFrom(t)) { UnityEngine.Object[] objs = UnityEngine.Object.FindObjectsOfType(t); count = objs.Length; if (count > 0) target = objs[0]; }
        lastOutput.AppendLine("inspect " + t.FullName + " live objects: " + count);
        for (Type x = t; x != null && x != typeof(MonoBehaviour) && x != typeof(object); x = x.BaseType)
            foreach (FieldInfo f in x.GetFields(ALL | BindingFlags.DeclaredOnly))
            {
                Type ft = f.FieldType;
                if (!(ft.IsPrimitive || ft.IsEnum)) continue;
                string val = "";
                try { val = (f.IsStatic || target != null) ? System.Convert.ToString(f.GetValue(f.IsStatic ? null : target), System.Globalization.CultureInfo.InvariantCulture) : "(no object)"; } catch { val = "?"; }
                lastOutput.AppendLine("  " + x.Name + "." + f.Name + " : " + ft.Name + (f.IsStatic ? " static" : "") + " = " + val);
            }
    }

    void WriteState()
    {
        StringBuilder sb = new StringBuilder();
        sb.AppendLine("time " + Time.unscaledTime.ToString("F1", System.Globalization.CultureInfo.InvariantCulture) + "  scene " + UnityEngine.SceneManagement.SceneManager.GetActiveScene().name);
        foreach (Cheat c in cheats)
            sb.AppendLine("cheat " + c.Name + " | " + (c.On ? "ON" : "off") + " | value=" + Read(c) + (c.Error.Length > 0 ? " | error=" + c.Error : ""));
        sb.Append(lastOutput.ToString());
        try { File.WriteAllText(statePath, sb.ToString()); } catch { }
    }

    void OnGUI()
    {
        if (!showOverlay || cheats.Count == 0) return;
        float h = 26 + cheats.Count * 20;
        GUI.Box(new Rect(10, 10, 330, h), "Trainer  (Insert: hide)");
        for (int i = 0; i < cheats.Count; i++)
        {
            Cheat c = cheats[i];
            string state = (c.Mode == "freeze" || c.Mode == "lock") ? (c.On ? "ON " : "off") : "   ";
            GUI.Label(new Rect(18, 30 + i * 20, 320, 20), "[" + c.Hotkey + "] " + state + "  " + c.Name + (c.Error.Length > 0 && c.On ? "  (" + c.Error + ")" : ""));
        }
    }
}
