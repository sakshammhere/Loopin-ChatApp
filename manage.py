from database import supabase

def test_connection():
    try:
        res = supabase.table("users").select("*").limit(1).execute()
        print("✅ Database connection working!")
        print(res.data)
    except Exception as e:
        print("❌ Database connection failed:", e)

if __name__ == "__main__":
    test_connection()
