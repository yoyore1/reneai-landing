export default function ReneAILanding() {
  return (
    <div style={{
      color: "white",
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      padding: "0 1.5rem",
      backgroundColor: "rgba(13, 15, 20, 0.75)",
      backdropFilter: "blur(5px)",
    }}>
      {/* === Header === */}
      <header
        style={{
          width: "100%",
          padding: "1rem 1.5rem",
          display: "flex",
          justifyContent: "center",
          borderBottom: "1px solid rgba(255,255,255,0.1)",
          backgroundColor: "rgba(13, 15, 20, 0.8)",
          position: "sticky",
          top: 0,
          zIndex: 50,
          cursor: "pointer",
        }}
        onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}
      >
        <h2 style={{ fontSize: "1.5rem", fontWeight: "bold", color: "#2D9CFF" }}>ReneAI</h2>
      </header>

      {/* === Hero Section === */}
      <section style={{ textAlign: "center", padding: "6rem 0 4rem", maxWidth: "48rem" }}>
        <h1 style={{ fontSize: "3rem", fontWeight: "bold", marginBottom: "1rem" }}>
          Your 24/7 AI Receptionist — Book, Cancel, Reschedule, and Answer Calls Instantly.
        </h1>
        <p style={{ fontSize: "1.125rem", color: "#E0E2E8", marginBottom: "1.75rem" }}>
          ReneAI handles your front desk so you can focus on your business.
        </p>
        <div style={{ display: "flex", justifyContent: "center", gap: "1rem" }}>
          <button style={{ backgroundColor: "#2D9CFF", color: "white", padding: "0.75rem 1.5rem", borderRadius: "0.75rem", border: "none", fontWeight: 600 }}>
            Get Started
          </button>
          <button style={{ border: "1px solid #2D9CFF", color: "#2D9CFF", padding: "0.75rem 1.5rem", borderRadius: "0.75rem", background: "none", fontWeight: 600 }}>
            Learn More
          </button>
        </div>
      </section>

      {/* === Experience Section === */}
      <section style={{
        maxWidth: "48rem",
        textAlign: "center",
        padding: "3rem 0 4rem",
        borderTop: "1px solid rgba(255,255,255,0.1)"
      }}>
        <h2 style={{ fontSize: "2.25rem", fontWeight: "bold", marginBottom: "1.25rem" }}>Experience ReneAI Live</h2>
        <p style={{ color: "#E0E2E8", fontSize: "1.125rem", marginBottom: "1.75rem", lineHeight: "1.6" }}>
          Watch our demo or call ReneAI directly to experience the assistant in action. 
          Discover how easy front-desk automation can be for your business.
        </p>
        <div style={{ display: "flex", justifyContent: "center", gap: "1rem", flexWrap: "wrap" }}>
          <a
            href="tel:+14376008812"
            style={{
              display: "inline-block",
              backgroundColor: "#2D9CFF",
              color: "white",
              padding: "1rem 2rem",
              borderRadius: "0.75rem",
              fontWeight: 600,
              textDecoration: "none",
              boxShadow: "0 0 10px rgba(45,156,255,0.3)",
            }}
          >
            📞 Call ReneAI Now
          </a>
          <a
            href="mailto:yovenre1@gmail.com"
            style={{
              display: "inline-block",
              border: "1px solid #2D9CFF",
              color: "#2D9CFF",
              padding: "1rem 2rem",
              borderRadius: "0.75rem",
              fontWeight: 600,
              textDecoration: "none",
              background: "none",
              boxShadow: "0 0 10px rgba(45,156,255,0.3)",
            }}
          >
            📅 Book a Demo
          </a>
        </div>
      </section>

      {/* === Features Section === */}
      <section style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(250px, 1fr))",
        gap: "2rem",
        maxWidth: "72rem",
        padding: "5rem 0",
        borderTop: "1px solid rgba(255,255,255,0.1)"
      }}>
        {[
          { title: "Automated scheduling.", desc: "ReneAI manages bookings, cancellations, and reschedules with ease." },
          { title: "Seamless reminders.", desc: "Sends confirmations, reminders, and outbound calls for follow-ups." },
          { title: "24/7 availability.", desc: "Natural speech, Google integration, and industry versatility built in." },
        ].map((f, i) => (
          <div key={i} style={{
            background: "rgba(23, 26, 34, 0.7)",
            border: "1px solid rgba(255,255,255,0.1)",
            borderRadius: "1rem",
            padding: "2rem",
            textAlign: "left",
            backdropFilter: "blur(3px)"
          }}>
            <h3 style={{ fontSize: "1.5rem", fontWeight: "600", marginBottom: "0.75rem", color: "white" }}>{f.title}</h3>
            <p style={{ color: "#C5C8CE", fontSize: "0.9rem", lineHeight: "1.5" }}>{f.desc}</p>
          </div>
        ))}
      </section>

      {/* === Privacy Section === */}
      <section style={{
        maxWidth: "48rem",
        textAlign: "center",
        padding: "6rem 0",
        borderTop: "1px solid rgba(255,255,255,0.1)"
      }}>
        <h2 style={{ fontSize: "2.25rem", fontWeight: "bold", marginBottom: "1.5rem" }}>Privacy & Terms</h2>
        <p style={{ color: "#C5C8CE", fontSize: "1.125rem", lineHeight: "1.6", marginBottom: "1.5rem" }}>
          ReneAI respects your privacy. We do not collect or store personal information through this website. 
          Phone calls are securely handled by Twilio, following encryption and data-protection practices.
        </p>
        <p style={{ color: "#C5C8CE", fontSize: "1rem", lineHeight: "1.6" }}>
          For privacy or data questions, contact us at{" "}
          <a href="mailto:support@reneai.com" style={{ color: "#2D9CFF", textDecoration: "none" }}>
            support@reneai.com
          </a>.
        </p>
      </section>

      {/* === Footer === */}
      <footer style={{
        color: "#8B90A0",
        fontSize: "0.875rem",
        padding: "4rem 0",
        borderTop: "1px solid rgba(255,255,255,0.1)",
        width: "100%",
        maxWidth: "72rem",
        textAlign: "center"
      }}>
        <p>support@reneai.com</p>
        <p style={{ marginTop: "0.5rem" }}>
          © {new Date().getFullYear()} ReneAI — All rights reserved.
        </p>
      </footer>
    </div>
  );
}
