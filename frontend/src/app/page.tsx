import Image from "next/image";

export default function Home() {
  return (
    <div className="flex flex-1 flex-col items-center justify-center min-h-screen gap-8">
      <Image
        src="/logo.svg"
        alt="duckstring"
        width={320}
        height={320}
        priority
      />
      <div className="flex flex-col items-center gap-2 text-center">
        <h1 className="text-2xl font-semibold tracking-widest text-white uppercase">
          duckstring
        </h1>
        <p className="text-sm tracking-wider text-zinc-500 uppercase">
          in development
        </p>
      </div>
    </div>
  );
}
