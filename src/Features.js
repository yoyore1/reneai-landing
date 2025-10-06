import Navbar from "./Navbar";
import Footer from "./Footer";

export default function Features() {
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
        <h1 style={{ fontSize: "2.5rem", fontWeight: "bold", marginBottom: "1rem" }}>Features & Integrations</h1>
        <p style={{ color: "#C5C8CE", fontSize: "1.125rem", marginBottom: "3rem" }}>
          Dive deeper into the tech that powers ReneAI. Seamless automation, smart scheduling, and real-time integration.
        </p>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(250px, 1fr))", gap: "2rem" }}>
          {[
            { icon: "📅", title: "Google Calendar Sync", desc: "Automatic scheduling and availability updates." },
            { icon: "📞", title: "Twilio Voice", desc: "Crystal-clear AI-powered phone calls." },
            { icon: "💬", title: "SMS Confirmations", desc: "Texts for confirmations, reminders, and reschedules." },
          ].map((f, i) => (
            <div key={i} style={{
              background: "rgba(23,26,34,0.7)",
              border: "1px solid rgba(255,255,255,0.1)",
              borderRadius: "1rem",
              padding: "2rem",
            }}>
              <div style={{ fontSize: "2rem", marginBottom: "0.5rem" }}>{f.icon}</div>
              <h3 style={{ fontSize: "1.25rem", fontWeight: 600 }}>{f.title}</h3>
              <p style={{ color: "#C5C8CE", marginTop: "0.5rem" }}>{f.desc}</p>
            </div>
          ))}
        </div>
      </section>
      <Footer />
    </div>
  );
}
