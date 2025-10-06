import Navbar from "./Navbar";
import Footer from "./Footer";

export default function Industries() {
  return (
    <div style={{
      color: "white",
      backgroundColor: "rgba(13,15,20,0.9)",
      backdropFilter: "blur(5px)",
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      padding: "0 1.5rem",
    }}>
      <Navbar />
      <section style={{ textAlign: "center", padding: "6rem 0", maxWidth: "60rem" }}>
        <h1 style={{ fontSize: "2.5rem", fontWeight: "bold", marginBottom: "1rem" }}>Industries We Serve</h1>
        <p style={{ color: "#C5C8CE", fontSize: "1.125rem", marginBottom: "3rem" }}>
          ReneAI adapts to any business — from clinics to barbershops, restaurants, and auto shops.
        </p>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(250px, 1fr))", gap: "2rem" }}>
          {[
            { icon: "🦷", title: "Dental Clinics", desc: "Automate appointment bookings, confirmations, and cancellations." },
            { icon: "💈", title: "Barbershops", desc: "Keep your chairs full with 24/7 scheduling and rescheduling." },
            { icon: "🍔", title: "Restaurants", desc: "Handle reservations, takeout, and inquiries instantly." },
            { icon: "🔧", title: "Auto Shops", desc: "Manage service bookings, reminders, and follow-ups automatically." },
          ].map((i, idx) => (
            <div key={idx} style={{
              background: "rgba(23,26,34,0.7)",
              border: "1px solid rgba(255,255,255,0.1)",
              borderRadius: "1rem",
              padding: "2rem",
            }}>
              <div style={{ fontSize: "2rem", marginBottom: "0.5rem" }}>{i.icon}</div>
              <h3 style={{ fontSize: "1.25rem", fontWeight: 600 }}>{i.title}</h3>
              <p style={{ color: "#C5C8CE", marginTop: "0.5rem" }}>{i.desc}</p>
            </div>
          ))}
        </div>
      </section>
      <Footer />
    </div>
  );
}
