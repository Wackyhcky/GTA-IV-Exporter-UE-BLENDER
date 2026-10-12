#include "GTAIVTrafficManager.h"

#include "GTAIVTrafficCar.h"
#include "Engine/World.h"
#include "GameFramework/Pawn.h"
#include "GameFramework/PlayerController.h"
#include "Camera/PlayerCameraManager.h"

namespace
{
	constexpr float CellSize = 10000.f;      // spawn-lookup grid (100 m)
	constexpr float CornerRadius = 600.f;    // how far before/after a node turns are rounded
	constexpr float LookAhead = 6000.f;      // route kept this far ahead of each car
	// Intelligent Driver Model
	constexpr float MaxAccel = 300.f;        // cm/s^2
	constexpr float ComfortBrake = 350.f;
	constexpr float MinGap = 250.f;
	constexpr float TimeHeadway = 1.2f;
	constexpr float StopLineBack = 800.f;    // stop this far before a junction's corner

	float SegLen(const FVector& A, const FVector& B) { return FVector::Dist(A, B); }

	FVector Flat(const FVector& V)
	{
		FVector F(V.X, V.Y, 0.f);
		return F.GetSafeNormal();
	}
}

AGTAIVTrafficManager::AGTAIVTrafficManager()
{
	PrimaryActorTick.bCanEverTick = true;
	PrimaryActorTick.TickGroup = TG_PrePhysics;
	RootComponent = CreateDefaultSubobject<USceneComponent>(TEXT("Root"));
}

void AGTAIVTrafficManager::BeginPlay()
{
	Super::BeginPlay();
	Rng.GenerateNewSeed();
	BuildCaches();
}

void AGTAIVTrafficManager::EndPlay(const EEndPlayReason::Type Reason)
{
	ClearTraffic();
	Super::EndPlay(Reason);
}

void AGTAIVTrafficManager::ClearTraffic()
{
	for (int32 i = Agents.Num() - 1; i >= 0; --i)
	{
		RemoveAgent(i);
	}
}

void AGTAIVTrafficManager::BuildCaches()
{
	InLinks.Reset();
	bJunction.Reset();
	JunctionAxis.Reset();
	Grid.Reset();
	VehicleWeights.Reset();
	if (!Network)
	{
		return;
	}
	const int32 N = Network->Nodes.Num();
	InLinks.Init(0, N);
	TArray<TSet<int32>> Neighbours;
	Neighbours.SetNum(N);
	for (int32 i = 0; i < N; ++i)
	{
		const int32 First = Network->FirstLink.IsValidIndex(i) ? Network->FirstLink[i] : 0;
		const int32 Count = Network->LinkCount.IsValidIndex(i) ? Network->LinkCount[i] : 0;
		for (int32 k = First; k < First + Count && Network->LinkTarget.IsValidIndex(k); ++k)
		{
			const int32 T = Network->LinkTarget[k];
			if (T >= 0 && T < N)
			{
				InLinks[T]++;
				Neighbours[i].Add(T);
				Neighbours[T].Add(i);
			}
		}
		if (Count > 0)
		{
			const FVector& P = Network->Nodes[i];
			Grid.FindOrAdd(FIntPoint(FMath::FloorToInt(P.X / CellSize), FMath::FloorToInt(P.Y / CellSize))).Add(i);
		}
	}
	bJunction.Init(false, N);
	JunctionAxis.Init(FVector::ForwardVector, N);
	for (int32 i = 0; i < N; ++i)
	{
		// signals only at crossroads; at T-junctions and driveways cars just avoid each other
		bJunction[i] = Neighbours[i].Num() >= 4;
		for (int32 T : Neighbours[i])
		{
			JunctionAxis[i] = Flat(Network->Nodes[T] - Network->Nodes[i]);
			break;
		}
	}
	if (VehicleSet)
	{
		for (const FGTAIVVehicleDef& D : VehicleSet->Vehicles)
		{
			VehicleWeights.Add(D.Body ? FMath::Max(0.f, D.Frequency) : 0.f);
		}
	}
}

int32 AGTAIVTrafficManager::PickVehicle()
{
	float Total = 0.f;
	for (float W : VehicleWeights)
	{
		Total += W;
	}
	if (Total <= 0.f)
	{
		return INDEX_NONE;
	}
	float R = Rng.FRandRange(0.f, Total);
	for (int32 i = 0; i < VehicleWeights.Num(); ++i)
	{
		R -= VehicleWeights[i];
		if (R <= 0.f && VehicleWeights[i] > 0.f)
		{
			return i;
		}
	}
	return VehicleWeights.Num() - 1;
}

int32 AGTAIVTrafficManager::ReverseLanes(int32 From, int32 To) const
{
	const int32 First = Network->FirstLink[To];
	for (int32 k = First; k < First + Network->LinkCount[To]; ++k)
	{
		if (Network->LinkTarget[k] == From)
		{
			return Network->LinkLanes[k];
		}
	}
	return 0;
}

FVector AGTAIVTrafficManager::LaneOffset(int32 From, int32 To, int32 Lane) const
{
	int32 Lanes = 1;
	const int32 First = Network->FirstLink[From];
	for (int32 k = First; k < First + Network->LinkCount[From]; ++k)
	{
		if (Network->LinkTarget[k] == To)
		{
			Lanes = FMath::Max(1, Network->LinkLanes[k]);
			break;
		}
	}
	const FVector D = Flat(Network->Nodes[To] - Network->Nodes[From]);
	const FVector Right(-D.Y, D.X, 0.f);   // drive on the right
	const int32 L = FMath::Clamp(Lane, 0, Lanes - 1);
	float Off;
	if (ReverseLanes(From, To) > 0)
	{
		Off = 50.f + (L + 0.5f) * LaneWidth;                  // two-way: right of the centre line
	}
	else
	{
		Off = ((L + 0.5f) - Lanes * 0.5f) * LaneWidth;        // one-way: lanes across the whole road
	}
	return Right * Off;
}

bool AGTAIVTrafficManager::AppendNextLink(FAgent& A)
{
	const int32 Node = A.LastNode;
	const int32 First = Network->FirstLink[Node];
	const int32 Count = Network->LinkCount[Node];
	TArray<int32, TInlineAllocator<6>> Options;
	TArray<float, TInlineAllocator<6>> Weights;
	const FVector DirIn = Flat(Network->Nodes[Node] - Network->Nodes[A.PrevNode]);
	for (int32 k = First; k < First + Count; ++k)
	{
		const int32 T = Network->LinkTarget[k];
		if (T == A.PrevNode || Network->LinkLanes[k] <= 0)
		{
			continue;
		}
		const float Straight = FVector::DotProduct(DirIn, Flat(Network->Nodes[T] - Network->Nodes[Node]));
		if (Straight < -0.5f)
		{
			continue; // no hairpins
		}
		Options.Add(k);
		Weights.Add(Straight > 0.7f ? 3.f : 1.f); // mostly carry on straight, sometimes turn
	}
	if (Options.Num() == 0)
	{
		return false;
	}
	float Total = 0.f;
	for (float W : Weights)
	{
		Total += W;
	}
	float R = Rng.FRandRange(0.f, Total);
	int32 Pick = Options.Last();
	for (int32 i = 0; i < Options.Num(); ++i)
	{
		R -= Weights[i];
		if (R <= 0.f)
		{
			Pick = Options[i];
			break;
		}
	}
	const int32 Next = Network->LinkTarget[Pick];
	const int32 LanesOut = FMath::Max(1, Network->LinkLanes[Pick]);
	A.Lane = FMath::Clamp(A.Lane, 0, LanesOut - 1);

	const FVector& P = Network->Nodes[Node];
	const FVector& PN = Network->Nodes[Next];
	const FVector OffIn = LaneOffset(A.PrevNode, Node, A.Lane);
	const FVector OffOut = LaneOffset(Node, Next, A.Lane);
	const FVector DirOut = (PN - P).GetSafeNormal();
	const float LenOut = SegLen(P, PN);
	const float ROut = FMath::Min(CornerRadius, LenOut * 0.3f);
	const int32 J = bJunction[Node] ? Node : INDEX_NONE;

	// rounded corner through the node, then along the next link up to its far corner
	A.Path.Add(P + (OffIn + OffOut) * 0.5f);
	A.PathJunction.Add(J);
	A.Path.Add(P + OffOut + DirOut * ROut);
	A.PathJunction.Add(J);
	A.Path.Add(PN + OffOut - DirOut * ROut);
	A.PathJunction.Add(INDEX_NONE);
	A.PrevNode = Node;
	A.LastNode = Next;
	return true;
}

void AGTAIVTrafficManager::ExtendPath(FAgent& A, float Ahead)
{
	for (int32 Guard = 0; Guard < 16; ++Guard)
	{
		float Remaining = -A.SegT;
		for (int32 i = 0; i + 1 < A.Path.Num(); ++i)
		{
			Remaining += SegLen(A.Path[i], A.Path[i + 1]);
		}
		if (Remaining >= Ahead || !AppendNextLink(A))
		{
			return;
		}
	}
}

bool AGTAIVTrafficManager::TrySpawn(const FVector& PlayerLoc, const FVector& ViewLoc, const FVector& ViewDir)
{
	const int32 CR = FMath::CeilToInt(SpawnRadius / CellSize);
	const int32 CX = FMath::FloorToInt(PlayerLoc.X / CellSize);
	const int32 CY = FMath::FloorToInt(PlayerLoc.Y / CellSize);
	for (int32 Attempt = 0; Attempt < 6; ++Attempt)
	{
		const TArray<int32>* Cell = Grid.Find(FIntPoint(CX + Rng.RandRange(-CR, CR), CY + Rng.RandRange(-CR, CR)));
		if (!Cell || Cell->Num() == 0)
		{
			continue;
		}
		const int32 S = (*Cell)[Rng.RandRange(0, Cell->Num() - 1)];
		const FVector PS = Network->Nodes[S];
		const float Dist = FVector::Dist2D(PS, PlayerLoc);
		if (Dist < MinSpawnDistance || Dist > SpawnRadius)
		{
			continue;
		}
		// don't pop in right in front of the camera
		const FVector ToNode = PS - ViewLoc;
		if (ToNode.Size() < 9000.f && FVector::DotProduct(ToNode.GetSafeNormal(), ViewDir) > 0.4f)
		{
			continue;
		}
		const int32 First = Network->FirstLink[S];
		const int32 Count = Network->LinkCount[S];
		if (Count <= 0)
		{
			continue;
		}
		const int32 K = First + Rng.RandRange(0, Count - 1);
		const int32 T = Network->LinkTarget[K];
		const int32 Lanes = FMath::Max(1, Network->LinkLanes[K]);
		const FVector PT = Network->Nodes[T];
		const float Len = SegLen(PS, PT);
		if (Len < 300.f)
		{
			continue;
		}
		FAgent A;
		A.PrevNode = S;
		A.LastNode = T;
		A.Lane = Rng.RandRange(0, Lanes - 1);
		const FVector Off = LaneOffset(S, T, A.Lane);
		const FVector Dir = (PT - PS).GetSafeNormal();
		const float R = FMath::Min(CornerRadius, Len * 0.3f);
		A.Path = { PS + Off + Dir * R, PT + Off - Dir * R };
		A.PathJunction = { INDEX_NONE, INDEX_NONE };
		A.SegT = Rng.FRandRange(0.f, FMath::Max(0.f, Len - 2.f * R));
		const FVector Loc = FMath::Lerp(A.Path[0], A.Path[1], A.SegT / FMath::Max(1.f, SegLen(A.Path[0], A.Path[1])));
		bool bClear = true;
		for (const FAgent& B : Agents)
		{
			if (FVector::DistSquared(B.Location, Loc) < FMath::Square(1500.f))
			{
				bClear = false;
				break;
			}
		}
		if (!bClear)
		{
			continue;
		}
		const int32 V = PickVehicle();
		if (V == INDEX_NONE)
		{
			return false;
		}
		const FGTAIVVehicleDef& Def = VehicleSet->Vehicles[V];
		FActorSpawnParameters Params;
		Params.SpawnCollisionHandlingOverride = ESpawnActorCollisionHandlingMethod::AlwaysSpawn;
		Params.Owner = this;
		AGTAIVTrafficCar* Car = GetWorld()->SpawnActor<AGTAIVTrafficCar>(Loc, Dir.Rotation(), Params);
		if (!Car)
		{
			return false;
		}
		FGTAIVPaint Paint;
		if (Def.Paints.Num() > 0)
		{
			Paint = Def.Paints[Rng.RandRange(0, Def.Paints.Num() - 1)];
		}
		Car->Setup(Def, Paint);
		A.Car = Car;
		A.Length = Def.Length;
		A.RideHeight = Def.RideHeight;
		A.Desired = CruiseSpeed * Rng.FRandRange(0.8f, 1.15f);
		A.Speed = A.Desired * 0.7f;
		A.Location = Loc;
		A.Forward = Dir;
		A.Yaw = Dir.Rotation().Yaw;
		ExtendPath(A, LookAhead);
		Agents.Add(MoveTemp(A));
		PlaceCar(Agents.Last(), 0.f, 0.f);
		return true;
	}
	return false;
}

bool AGTAIVTrafficManager::PhaseAllows(int32 Junction, const FVector& ApproachDir) const
{
	// every signal runs the same cycle, so traffic flows down an avenue in a "green wave"
	const float T = GetWorld()->GetTimeSeconds();
	const float Phase = FMath::Max(4.f, JunctionPhaseSeconds);
	const int32 Index = FMath::FloorToInt(T / Phase) % 2;
	const float Into = FMath::Fmod(T, Phase);
	if (Into > Phase - 3.f)
	{
		return false;  // amber/all-red: nobody new enters
	}
	const bool bAlongAxis = FMath::Abs(FVector::DotProduct(Flat(ApproachDir), JunctionAxis[Junction])) > 0.707f;
	return Index == 0 ? bAlongAxis : !bAlongAxis;
}

float AGTAIVTrafficManager::JunctionStopDistance(FAgent& A) const
{
	float Dist = -A.SegT;
	for (int32 i = 1; i < A.Path.Num(); ++i)
	{
		Dist += SegLen(A.Path[i - 1], A.Path[i]);
		if (Dist > 4000.f)
		{
			break;
		}
		const int32 J = A.PathJunction[i];
		if (J == INDEX_NONE)
		{
			continue;
		}
		if (J == A.GrantedJunction)
		{
			return BIG_NUMBER;
		}
		const float Stop = Dist - StopLineBack;
		const FVector Approach = A.Path[i] - A.Path[i - 1];
		// too close to stop comfortably (or already past the line): keep going
		const float BrakeDist = A.Speed * A.Speed / (2.f * ComfortBrake);
		if (Stop < BrakeDist * 0.6f && A.Speed > 300.f || Stop < 0.f)
		{
			A.GrantedJunction = J;
			return BIG_NUMBER;
		}
		if (PhaseAllows(J, Approach))
		{
			if (Stop < 1500.f)
			{
				A.GrantedJunction = J;
			}
			return BIG_NUMBER;
		}
		return FMath::Max(0.f, Stop);
	}
	return BIG_NUMBER;
}

float AGTAIVTrafficManager::GapAhead(const FAgent& A, int32 Index, const FVector& PlayerLoc, bool bHasPlayer, float& OutLeadSpeed) const
{
	float Best = BIG_NUMBER;
	OutLeadSpeed = 0.f;
	auto Probe = [&](const FVector& Loc, float HalfLen, float Radius, float LeadSpeed)
	{
		float Along = -A.SegT;
		for (int32 i = 0; i + 1 < A.Path.Num(); ++i)
		{
			const FVector P0 = A.Path[i];
			const FVector P1 = A.Path[i + 1];
			const float L = SegLen(P0, P1);
			if (Along > 4500.f)
			{
				break;
			}
			const FVector Closest = FMath::ClosestPointOnSegment(Loc, P0, P1);
			const float D = FVector::Dist2D(Closest, Loc);
			if (D < Radius && FMath::Abs(Loc.Z - Closest.Z) < 400.f)
			{
				const float At = Along + FVector::Dist(P0, Closest);
				if (At > 0.f)
				{
					const float Gap = At - A.Length * 0.5f - HalfLen;
					if (Gap < Best)
					{
						Best = Gap;
						OutLeadSpeed = LeadSpeed;
					}
				}
				return;
			}
			Along += L;
		}
	};
	for (int32 j = 0; j < Agents.Num(); ++j)
	{
		if (j == Index)
		{
			continue;
		}
		const FAgent& B = Agents[j];
		if (FVector::DistSquared(B.Location, A.Location) > FMath::Square(5000.f))
		{
			continue;
		}
		const float Lead = B.Speed * FMath::Max(0.f, FVector::DotProduct(B.Forward, A.Forward));
		Probe(B.Location, B.Length * 0.5f, 230.f, Lead);
	}
	if (bHasPlayer && bStopForPlayer && FVector::DistSquared(PlayerLoc, A.Location) < FMath::Square(5000.f))
	{
		Probe(PlayerLoc, 80.f, 260.f, 0.f);
	}
	return Best;
}

void AGTAIVTrafficManager::Advance(FAgent& A, float Distance)
{
	A.SegT += Distance;
	while (A.Path.Num() >= 2)
	{
		const float L = SegLen(A.Path[0], A.Path[1]);
		if (A.SegT < L)
		{
			break;
		}
		A.SegT -= L;
		A.Path.RemoveAt(0, 1, EAllowShrinking::No);
		A.PathJunction.RemoveAt(0, 1, EAllowShrinking::No);
	}
	if (A.Path.Num() < 2)
	{
		A.SegT = 0.f;
	}
}

FVector AGTAIVTrafficManager::PathPoint(const FAgent& A, float S) const
{
	float Acc = 0.f;
	for (int32 i = 0; i + 1 < A.Path.Num(); ++i)
	{
		const float L = SegLen(A.Path[i], A.Path[i + 1]);
		if (S <= Acc + L || i + 2 == A.Path.Num())
		{
			return FMath::Lerp(A.Path[i], A.Path[i + 1], FMath::Clamp((S - Acc) / FMath::Max(L, 1.f), 0.f, 1.f));
		}
		Acc += L;
	}
	return A.Path.Num() ? A.Path.Last() : A.Location;
}

void AGTAIVTrafficManager::PlaceCar(FAgent& A, float Dt, float Moved)
{
	AGTAIVTrafficCar* Car = A.Car.Get();
	if (!Car || A.Path.Num() < 2)
	{
		return;
	}
	// position, and a heading smoothed over a few metres of the path
	auto PointAt = [&A](float S)
	{
		float Acc = 0.f;
		for (int32 i = 0; i + 1 < A.Path.Num(); ++i)
		{
			const float L = SegLen(A.Path[i], A.Path[i + 1]);
			if (S <= Acc + L || i + 2 == A.Path.Num())
			{
				return FMath::Lerp(A.Path[i], A.Path[i + 1], FMath::Clamp((S - Acc) / FMath::Max(L, 1.f), 0.f, 1.f));
			}
			Acc += L;
		}
		return A.Path.Last();
	};
	const FVector Loc = PointAt(A.SegT);
	const FVector Ahead = PointAt(A.SegT + 350.f);
	const FVector Behind = PointAt(FMath::Max(0.f, A.SegT - 350.f));
	FVector Dir = (Ahead - Behind).GetSafeNormal();
	if (Dir.IsNearlyZero())
	{
		Dir = A.Forward;
	}
	const float TargetYaw = Dir.Rotation().Yaw;
	A.Yaw = Dt > 0.f ? FMath::FixedTurn(A.Yaw, TargetYaw, 220.f * Dt + 0.5f) : TargetYaw;
	const FVector Fwd = FRotator(0.f, A.Yaw, 0.f).Vector();

	// sit on the ground: trace under the front and rear axles
	float Z = Loc.Z;
	float Pitch = 0.f;
	const FVector Half = Fwd * A.Length * 0.35f;
	FHitResult HF, HR;
	FCollisionQueryParams Q(SCENE_QUERY_STAT(GTAIVTrafficGround), false, Car);
	const bool bF = GetWorld()->LineTraceSingleByChannel(HF, Loc + Half + FVector(0, 0, 400), Loc + Half - FVector(0, 0, 1200), ECC_Visibility, Q);
	const bool bR = GetWorld()->LineTraceSingleByChannel(HR, Loc - Half + FVector(0, 0, 400), Loc - Half - FVector(0, 0, 1200), ECC_Visibility, Q);
	if (bF && bR)
	{
		Z = (HF.ImpactPoint.Z + HR.ImpactPoint.Z) * 0.5f;
		Pitch = FMath::RadiansToDegrees(FMath::Atan2(HF.ImpactPoint.Z - HR.ImpactPoint.Z, A.Length * 0.7f));
	}
	else if (bF || bR)
	{
		Z = bF ? HF.ImpactPoint.Z : HR.ImpactPoint.Z;
	}
	A.Location = FVector(Loc.X, Loc.Y, Z);
	A.Forward = Fwd;
	Car->SetActorLocationAndRotation(FVector(Loc.X, Loc.Y, Z + A.RideHeight), FRotator(Pitch, A.Yaw, 0.f));
	const float Steer = FMath::Clamp(FMath::FindDeltaAngleDegrees(A.Yaw, (PointAt(A.SegT + 600.f) - Loc).Rotation().Yaw), -35.f, 35.f);
	Car->UpdateWheels(Moved, Steer);
}

void AGTAIVTrafficManager::UpdateAgent(FAgent& A, float Dt, const FVector& PlayerLoc, bool bHasPlayer, int32 Index)
{
	ExtendPath(A, LookAhead);

	// slow down for bends coming up: heading change between 8 m chords of the route
	// (short segments around nodes would otherwise read as sharp turns)
	float Turn = 0.f;
	{
		FVector Prev = PathPoint(A, A.SegT);
		FVector PrevDir = FVector::ZeroVector;
		for (float S = 800.f; S <= 3200.f; S += 800.f)
		{
			const FVector P = PathPoint(A, A.SegT + S);
			const FVector D = Flat(P - Prev);
			if (!D.IsNearlyZero() && !PrevDir.IsNearlyZero())
			{
				Turn = FMath::Max(Turn, FMath::RadiansToDegrees(FMath::Acos(FMath::Clamp(FVector::DotProduct(PrevDir, D), -1.f, 1.f))));
			}
			if (!D.IsNearlyZero())
			{
				PrevDir = D;
			}
			Prev = P;
		}
	}
	const float V0 = FMath::Max(200.f, A.Desired * FMath::Lerp(1.f, 0.4f, FMath::Clamp(Turn / 90.f, 0.f, 1.f)));
	const float V = A.Speed;
	float Acc = MaxAccel * (1.f - FMath::Pow(V / V0, 4.f));

	auto Interact = [&](float Gap, float LeadSpeed)
	{
		if (Gap >= BIG_NUMBER * 0.5f)
		{
			return;
		}
		const float Dv = V - LeadSpeed;
		const float SStar = MinGap + V * TimeHeadway + V * Dv / (2.f * FMath::Sqrt(MaxAccel * ComfortBrake));
		Acc = FMath::Min(Acc, MaxAccel * (1.f - FMath::Pow(V / V0, 4.f)) - MaxAccel * FMath::Square(FMath::Max(SStar, 0.f) / FMath::Max(Gap, 10.f)));
	};
	float LeadSpeed = 0.f;
	const float Gap = GapAhead(A, Index, PlayerLoc, bHasPlayer, LeadSpeed);
	Interact(Gap, LeadSpeed);
	Interact(JunctionStopDistance(A) + MinGap, 0.f);

	Acc = FMath::Clamp(Acc, -1200.f, MaxAccel);
	A.Speed = FMath::Max(0.f, A.Speed + Acc * Dt);
	const float Moved = A.Speed * Dt;
	Advance(A, Moved);
	A.StuckTime = A.Speed < 20.f ? A.StuckTime + Dt : 0.f;
	PlaceCar(A, Dt, Moved);
}

void AGTAIVTrafficManager::RemoveAgent(int32 Index)
{
	if (AGTAIVTrafficCar* Car = Agents[Index].Car.Get())
	{
		Car->Destroy();
	}
	Agents.RemoveAtSwap(Index);
}

void AGTAIVTrafficManager::Tick(float DeltaSeconds)
{
	Super::Tick(DeltaSeconds);
	if (!Network || !VehicleSet || Network->Nodes.Num() == 0)
	{
		return;
	}
	if (bJunction.Num() != Network->Nodes.Num())
	{
		BuildCaches();
	}
	// long frames are split into small steps (rather than clamped) so cars keep real-time speed
	const float FrameDt = FMath::Min(DeltaSeconds, 0.5f);
	const int32 Steps = FMath::Max(1, FMath::CeilToInt(FrameDt / 0.05f));
	const float Dt = FrameDt / Steps;

	FVector PlayerLoc = GetActorLocation();
	FVector ViewLoc = PlayerLoc;
	FVector ViewDir = FVector::ForwardVector;
	bool bHasPlayer = false;
	if (APlayerController* PC = GetWorld()->GetFirstPlayerController())
	{
		if (PC->PlayerCameraManager)
		{
			ViewLoc = PC->PlayerCameraManager->GetCameraLocation();
			ViewDir = PC->PlayerCameraManager->GetCameraRotation().Vector();
			PlayerLoc = ViewLoc;
		}
		if (APawn* Pawn = PC->GetPawn())
		{
			PlayerLoc = Pawn->GetActorLocation();
			bHasPlayer = true;
		}
	}

	// move everyone, then retire cars that are far away, stuck out of sight or out of road
	for (int32 Step = 0; Step < Steps; ++Step)
	{
		for (int32 i = 0; i < Agents.Num(); ++i)
		{
			UpdateAgent(Agents[i], Dt, PlayerLoc, bHasPlayer, i);
		}
	}
	for (int32 i = Agents.Num() - 1; i >= 0; --i)
	{
		const FAgent& A = Agents[i];
		const float Dist = FVector::Dist2D(A.Location, PlayerLoc);
		const bool bEndOfRoad = A.Path.Num() < 2;
		const bool bStuck = A.StuckTime > 40.f && Dist > 6000.f;
		if (!A.Car.IsValid() || Dist > DespawnRadius || bEndOfRoad || bStuck || !bTrafficEnabled)
		{
			RemoveAgent(i);
		}
	}

	if (bTrafficEnabled)
	{
		SpawnTimer += FrameDt;
		int32 Budget = 2;
		while (SpawnTimer > 0.05f && Agents.Num() < MaxCars && Budget-- > 0)
		{
			SpawnTimer -= 0.05f;
			TrySpawn(PlayerLoc, ViewLoc, ViewDir);
		}
		SpawnTimer = FMath::Min(SpawnTimer, 0.2f);
	}
}
