import Navbar from "./Navbar";
import Footer from "./Footer";

export default function About() {
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
        <h1 style={{ fontSize: "2.5rem", fontWeight: "bold", marginBottom: "1rem" }}>About ReneAI</h1>
        <p style={{ color: "#C5C8CE", fontSize: "1.125rem", lineHeight: "1.8", marginBottom: "2rem" }}>
          ReneAI was built to give small businesses enterprise-level automation — affordable, reliable, and human-like. 
          Our mission is to modernize how businesses handle calls and scheduling without losing personal touch.
        </p>
        <p style={{ color: "#C5C8CE", fontSize: "1.125rem", lineHeight: "1.8" }}>
          Founded by innovators passionate about AI and customer service, ReneAI brings together cutting-edge technology and simple design to save time, boost productivity, and help small teams scale effortlessly.
        </p>
      </section>
      <Footer />
    </div>
  );
}
