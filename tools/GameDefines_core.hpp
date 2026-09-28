// ---- core types and engine access (appended verbatim by uegen; everything below is driven by the defines above) ----
// Works in-process (PRX on the console): pointers are dereferenced directly. Sizes marked SIZEOF_* were measured
// from the dump so the generated class layouts line up.
#include <cstddef>
#include <cstring>
#include <string>

#ifndef UE_MODULE_BASE
#define UE_MODULE_BASE 0x400000ULL          // eboot.bin text base on PS4; override before including if needed
#endif
#define UE_ADDR(off) ((uintptr_t)UE_MODULE_BASE + (uintptr_t)(off))

template<typename T>
struct TArray
{
	T* Data;
	int32_t Num;
	int32_t Max;

	int32_t size() const { return Num; }
	T& operator[](int32_t i) { return Data[i]; }
	const T& operator[](int32_t i) const { return Data[i]; }
	bool valid(int32_t i) const { return Data && i >= 0 && i < Num; }
	T* begin() { return Data; }
	T* end() { return Data + Num; }
};
static_assert(sizeof(TArray<int>) == 16, "TArray layout");

struct FName
{
	int32_t Index;
#if OFF_FName_Number == 8
	int32_t DisplayIndex;
#endif
	int32_t Number;
#if OFF_FName_Size == 16 && OFF_FName_Number != 8
	int32_t Pad[2];
#elif OFF_FName_Size == 16
	int32_t Pad;
#endif
	std::string ToString() const;               // resolved through GNames (see UE::NameToString)
	bool operator==(const FName& o) const { return Index == o.Index && Number == o.Number; }
};
static_assert(sizeof(FName) == OFF_FName_Size, "FName size");

class FString : public TArray<wchar_t>          // UE3: TArray<UNICHAR>; UE4: TArray<TCHAR>, both 16-bit on PS4
{
public:
	std::string ToString() const
	{
		std::string s;
		for (int32_t i = 0; Data && i < Num && Data[i]; ++i)
			s += (char)(Data[i] < 0x80 ? Data[i] : '?');
		return s;
	}
};
static_assert(sizeof(FString) == SIZEOF_StrProperty, "FString size");

struct FScriptDelegate { uint8_t Raw[SIZEOF_DelegateProperty]; };
struct FMulticastScriptDelegate { uint8_t Raw[SIZEOF_MulticastDelegate]; };
struct FSparseDelegate { uint8_t Raw[SIZEOF_SparseDelegate]; };
template<typename T> struct TScriptInterface { uint8_t Raw[SIZEOF_InterfaceProperty]; };
template<typename K, typename V> struct TMap { uint8_t Raw[SIZEOF_MapProperty]; };
template<typename T> struct TSet { uint8_t Raw[SIZEOF_SetProperty]; };
class FText { public: uint8_t Raw[SIZEOF_TextProperty]; };
template<typename T> struct TWeakObjectPtr { int32_t ObjectIndex; int32_t ObjectSerialNumber; };
template<typename T> struct TLazyObjectPtr { uint8_t Raw[SIZEOF_LazyObjectProperty]; };
template<typename T> struct TSoftObjectPtr { uint8_t Raw[SIZEOF_SoftObjectProperty]; };
template<typename T> struct TSoftClassPtr { uint8_t Raw[SIZEOF_SoftObjectProperty]; };
struct TFieldPath { uint8_t Raw[SIZEOF_FieldPathProperty]; };
template<typename T> struct TOptional { T Value; bool bIsSet; };
class FUtf8String : public TArray<char> {};
class FAnsiString : public TArray<char> {};

class UObject;
class UClass;
class UFunction;

namespace UE
{
	// ---- names ------------------------------------------------------------------------------------------------
	inline std::string NameToString(int32_t index, int32_t number)
	{
		std::string s;
#if UE_NAMES_KIND == 6
		// Mass Effect Legendary Edition: GNames = 8 pool base pointers, index = pool << 29 | byte offset,
		// entry = {u32 hash|flags (bit 30 = wide); FNameEntry* next @+4; name @+0xc}
		uint8_t* base = ((uint8_t**)UE_ADDR(GNames_Offset))[((uint32_t)index >> 29) & 7];
		if (!base) return "None";
		uint8_t* e = base + ((uint32_t)index & 0x1fffffff);
		bool wide = (*(uint32_t*)e) & 0x40000000;
		if (wide) { const wchar_t* w = (const wchar_t*)(e + NameEntry_StrOffset); for (; *w; ++w) s += (char)(*w < 0x80 ? *w : '?'); }
		else s = (const char*)(e + NameEntry_StrOffset);
#elif UE_NAMES_KIND == 3 || UE_NAMES_KIND == 4
		// TArray<FNameEntry*> (UE3) or TNameEntryArray chunks (UE4 < 4.23); entry = {int32 idx<<1|wide, pad, next, name}
#if UE_NAMES_KIND == 3
		TArray<uint8_t*>* names = (TArray<uint8_t*>*)UE_ADDR(GNames_Offset);
		if (!names || !names->Data || index < 0 || index >= names->Num) return "None";
		uint8_t* e = names->Data[index];
#else
		uint8_t*** chunks = *(uint8_t****)UE_ADDR(GNames_Offset);
		if (!chunks || index < 0) return "None";
		uint8_t** chunk = chunks[index / 16384];
		uint8_t* e = chunk ? chunk[index % 16384] : nullptr;
#endif
		if (!e) return "None";
		bool wide = (*(int32_t*)e) & 1;
		if (wide) { const wchar_t* w = (const wchar_t*)(e + NameEntry_StrOffset); for (; *w; ++w) s += (char)(*w < 0x80 ? *w : '?'); }
		else s = (const char*)(e + NameEntry_StrOffset);
#else
		// FNamePool (UE 4.23+): Blocks[] at +0x10, entries of 2-byte stride, header u16 {len<<6 | 5 hash bits | wide}
		uint8_t* pool = (uint8_t*)UE_ADDR(GNames_Offset);
		uint8_t* block = *(uint8_t**)(pool + 0x10 + 8 * (index >> FNamePool_BlockBits));
		if (!block) return "None";
		uint8_t* e = block + (index & ((1 << FNamePool_BlockBits) - 1)) * 2;
		uint16_t hdr = *(uint16_t*)e;
		int32_t len = hdr >> 6; bool wide = hdr & 1;
		if (len == 0) {                                                       // outline number entry
			int32_t i2 = *(int32_t*)(e + 2), num2 = *(int32_t*)(e + 6);
			return NameToString(i2, num2);
		}
		if (wide) { const wchar_t* w = (const wchar_t*)(e + NameEntry_StrOffset); for (int32_t i = 0; i < len; ++i) s += (char)(w[i] < 0x80 ? w[i] : '?'); }
		else s.assign((const char*)(e + NameEntry_StrOffset), len);
#endif
		if (number > 0) s += "_" + std::to_string(number - 1);
		return s;
	}

	// ---- objects ----------------------------------------------------------------------------------------------
	inline int32_t ObjectCount()
	{
#if UE_ENGINE == 3
		return ((TArray<UObject*>*)UE_ADDR(GObjects_Offset))->Num;
#elif defined(GObjects_Flat)
		return *(int32_t*)(UE_ADDR(GObjects_Offset) + 12);
#else
		return *(int32_t*)(UE_ADDR(GObjects_Offset) + GObjects_Layout_Num);
#endif
	}

	inline UObject* ObjectAt(int32_t i)
	{
#if UE_ENGINE == 3
		TArray<UObject*>* arr = (TArray<UObject*>*)UE_ADDR(GObjects_Offset);
		return arr->valid(i) ? arr->Data[i] : nullptr;
#elif defined(GObjects_Flat)
		uint8_t* objects = *(uint8_t**)UE_ADDR(GObjects_Offset);
		return (objects && i >= 0 && i < ObjectCount()) ? *(UObject**)(objects + i * FUObjectItem_Size) : nullptr;
#else
		uintptr_t a = UE_ADDR(GObjects_Offset);
		uint8_t** objects = *(uint8_t***)(a + GObjects_Layout_Objects);
		int32_t max = *(int32_t*)(a + GObjects_Layout_Max), maxChunks = *(int32_t*)(a + GObjects_Layout_MaxChunks);
		if (!objects || i < 0 || i >= ObjectCount() || maxChunks <= 0) return nullptr;
		int32_t perChunk = max / maxChunks;
		uint8_t* chunk = objects[i / perChunk];
		return chunk ? *(UObject**)(chunk + (i % perChunk) * FUObjectItem_Size) : nullptr;
#endif
	}

	inline UClass*  ClassOf(const UObject* o) { return *(UClass**)((uintptr_t)o + OFF_UObject_Class); }
	inline UObject* OuterOf(const UObject* o) { return *(UObject**)((uintptr_t)o + OFF_UObject_Outer); }
	inline FName    NameOf(const UObject* o)  { return *(FName*)((uintptr_t)o + OFF_UObject_Name); }
	inline UObject* SuperOf(const UObject* s) { return *(UObject**)((uintptr_t)s + OFF_UStruct_SuperStruct); }

	inline std::string GetName(const UObject* o) { FName n = NameOf(o); return NameToString(n.Index, n.Number); }

	inline std::string GetFullName(const UObject* o)
	{
		if (!o) return "None";
		std::string path = GetName(o);
		for (UObject* x = OuterOf(o); x; x = OuterOf(x)) path = GetName(x) + "." + path;
		return GetName((UObject*)ClassOf(o)) + " " + path;
	}

	inline bool IsA(const UObject* o, const UClass* cls)
	{
		if (!o || !cls) return false;
		for (UObject* c = (UObject*)ClassOf(o); c; c = SuperOf(c))
			if (c == (const UObject*)cls) return true;
		return false;
	}

	// "Class Engine.Actor" / "Function Engine.Actor.Tick" style lookup by full name (slow: linear scan; cache the result)
	inline UObject* FindObject(const std::string& fullName)
	{
		for (int32_t i = 0, n = ObjectCount(); i < n; ++i)
		{
			UObject* o = ObjectAt(i);
			if (o && GetFullName(o) == fullName) return o;
		}
		return nullptr;
	}
	inline UClass* FindClass(const std::string& fullName) { return (UClass*)FindObject(fullName); }

	template<typename T> inline T* FindFirst(const UClass* cls, bool skipDefaults = true)
	{
		for (int32_t i = 0, n = ObjectCount(); i < n; ++i)
		{
			UObject* o = ObjectAt(i);
			if (o && IsA(o, cls) && !(skipDefaults && GetName(o).compare(0, 9, "Default__") == 0)) return (T*)o;
		}
		return nullptr;
	}

	// ---- calls -------------------------------------------------------------------------------------------------
	// obj->ProcessEvent(function, params) through the object's vtable. UE3: functions with an iNative opcode number
	// return without executing; anything that creates objects must run on the game thread.
	inline void ProcessEvent(UObject* obj, UFunction* fn, void* params)
	{
		typedef void (*PE_t)(UObject*, UFunction*, void*, void*);
		PE_t pe = (*(PE_t**)obj)[ProcessEvent_Index];
		pe(obj, fn, params, nullptr);
	}
}

inline std::string FName::ToString() const { return UE::NameToString(Index, Number); }
